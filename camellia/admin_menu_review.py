"""Authenticated Menu Manager review routes and its small table editor."""
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from camellia import menu_dates, menu_review, weekly_savoury

router = APIRouter(prefix="/admin/menu-manager/menus/{menu_key}/review")


def reviewer(x_admin_key: Optional[str] = Header(None), x_admin_pin: Optional[str] = Header(None)):
    from camellia.app import require_admin, _admin_context
    require_admin(x_admin_key, x_admin_pin)
    context = _admin_context.get() or {}
    # Never store the API credential as an audit identity.
    return str(context.get("username") or "admin-api")[:120]


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except menu_review.ReviewConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "Menu, draft or version not found.") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
def get_review(menu_key: str, actor=Depends(reviewer)):
    return {"draft": call(menu_review.get_draft, menu_key),
            "categories": call(menu_review.categories, menu_key)}


@router.post("/extract-text")
def extract_text(menu_key: str, body: dict = Body(...), actor=Depends(reviewer)):
    return {"draft": call(menu_review.extract, menu_key, actor, text=body.get("text"))}


@router.post("/extract-file")
async def extract_file(menu_key: str, file: UploadFile = File(...), actor=Depends(reviewer)):
    data = await file.read(menu_review.MAX_BYTES + 1)
    if len(data) > menu_review.MAX_BYTES:
        raise HTTPException(413, "Menu files must be at most 10MB.")
    return {"draft": await run_in_threadpool(call, menu_review.extract, menu_key, actor,
            data=data, mime=file.content_type, filename=file.filename or "")}


@router.put("")
def save_review(menu_key: str, body: dict = Body(...), actor=Depends(reviewer)):
    return {"draft": call(menu_review.edit, menu_key, body.get("draft_id"), body.get("revision"), body.get("rows"), actor, body.get("menu_metadata"))}


@router.post("/reject")
def reject_review(menu_key: str, body: dict = Body(...), actor=Depends(reviewer)):
    return {"draft": call(menu_review.reject, menu_key, body.get("draft_id"), body.get("revision"), actor)}


@router.post("/approve-publish")
def approve_review(menu_key: str, body: dict = Body(...), actor=Depends(reviewer)):
    return {"draft": call(menu_review.publish_approved_menu, menu_key, body.get("draft_id"), body.get("revision"), actor)}


@router.get("/source/{draft_id}")
def review_source(menu_key: str, draft_id: str, actor=Depends(reviewer)):
    path = call(menu_review.source_file, menu_key, draft_id)
    # PDF preview uses the inert rendered flyer, not an active PDF document.
    if path.suffix == ".pdf":
        draft = menu_review.get_draft(menu_key)
        path = path.parent / draft["flyer_asset"]
    return FileResponse(path, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/versions")
def review_versions(menu_key: str, limit: int = Query(20, ge=1, le=99),
                    before_id: Optional[int] = Query(None, ge=1), actor=Depends(reviewer)):
    versions = call(menu_review.version_history, menu_key, limit + 1, before_id)
    more = len(versions) > limit
    versions = versions[:limit]
    return {"versions": versions, "has_more": more,
            "next_before_id": versions[-1]["id"] if more else None}


@router.get("/versions/{version_id}/source")
def version_source(menu_key: str, version_id: int, actor=Depends(reviewer)):
    path = call(menu_review.version_source_file, menu_key, version_id)
    return FileResponse(path, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/retry-extraction")
async def retry_review(menu_key: str, body: dict = Body(...), actor=Depends(reviewer)):
    draft = await run_in_threadpool(call, menu_review.retry_extraction, menu_key,
                                   body.get("draft_id"), body.get("revision"), actor)
    return {"draft": draft}


@router.post("/versions/{version_id}/restore")
def restore_review(menu_key: str, version_id: int, actor=Depends(reviewer)):
    return {"draft": call(menu_review.restore_as_draft, menu_key, version_id, actor)}


weekly_router = APIRouter(prefix="/admin/weekly-savoury")


def _weekly_view():
    versions = weekly_savoury.lifecycle_versions()
    current = weekly_savoury.current_version()
    upcoming = [version for version in versions if weekly_savoury.version_status(version) == "UPCOMING"]
    upcoming.sort(key=lambda version: version["valid_from"])
    def present(version):
        if not version:
            return None
        approval = json.loads(version["approval_json"])
        return {"version_id": version["id"], "status": weekly_savoury.version_status(version)
                if version.get("valid_from") else "CURRENT",
                "valid_from": version.get("valid_from"), "valid_until": version.get("valid_until"),
                "dates_expired": bool(version.get("valid_until") and version["valid_until"] < menu_dates.today_ist().isoformat()),
                "display_date_range": weekly_savoury.display_range(version["valid_from"], version["valid_until"])
                if version.get("valid_from") else (approval.get("menu_metadata") or {}).get("week_of"),
                "date_provenance": version.get("date_provenance"),
                "item_count": len(approval.get("rows") or []),
                "source_type": approval.get("source_type"), "uploaded_by": version.get("changed_by"),
                "menu_key": version["menu_key"], "has_source": bool(approval.get("source_asset")),
                "approved_by": approval.get("approved_by") or version.get("changed_by"),
                "changed_at": version.get("changed_at")}
    shown_current = present(current)
    if not shown_current and weekly_savoury.legacy_baseline_current():
        from camellia.config import config
        with open(config.MENU_JSON_PATH, encoding="utf-8") as stream:
            week = (json.load(stream).get("weekly_menus") or [{}])[-1]
        dates = menu_dates.parse_week_date_range(week.get("week_of"))
        shown_current = {"version_id": None, "status": "CURRENT",
            "valid_from": dates[0].isoformat(), "valid_until": dates[1].isoformat(),
            "display_date_range": weekly_savoury.display_range(*dates),
            "item_count": sum(len(week.get(section) or []) for section in menu_review.SECTIONS),
            "source_type": "existing", "uploaded_by": "Admin", "changed_at": None}
    reminder = None
    if shown_current and shown_current.get("valid_until"):
        end = datetime.combine(date.fromisoformat(shown_current["valid_until"]), time.max,
                               tzinfo=menu_dates._IST)
        if timedelta(0) <= end - datetime.now(menu_dates._IST) <= timedelta(hours=24):
            reminder = "Next menu is already scheduled." if upcoming else "Weekly Savoury expires within 24 hours. Upload next week's menu."
    return {"today": menu_dates.today_ist().isoformat(),
            "current": shown_current, "upcoming": [present(version) for version in upcoming],
            "versions": [present(version) for version in versions], "reminder": reminder,
            "draft": menu_review.get_draft(weekly_savoury.WEEKLY_KEY)}


@weekly_router.get("")
def weekly_status(actor=Depends(reviewer)):
    return _weekly_view()


@weekly_router.post("/retry-extraction")
async def weekly_retry(body: dict = Body(...), actor=Depends(reviewer)):
    draft = await run_in_threadpool(call, menu_review.retry_extraction, weekly_savoury.WEEKLY_KEY,
                                   body.get("draft_id"), body.get("revision"), actor)
    return {"draft": draft, "analysis": call(menu_review.weekly_analysis, draft)}


@weekly_router.post("/extract-text")
def weekly_extract_text(body: dict = Body(...), actor=Depends(reviewer)):
    draft = call(menu_review.extract, weekly_savoury.WEEKLY_KEY, actor, text=body.get("text"), weekly=True)
    return {"draft": draft, "analysis": call(menu_review.weekly_analysis, draft) if draft["status"] != "FAILED" else None}


@weekly_router.post("/extract-file")
async def weekly_extract_file(file: UploadFile = File(...), actor=Depends(reviewer)):
    data = await file.read(menu_review.MAX_BYTES + 1)
    if len(data) > menu_review.MAX_BYTES:
        raise HTTPException(413, "Menu files must be at most 10MB.")
    draft = await run_in_threadpool(call, menu_review.extract, weekly_savoury.WEEKLY_KEY, actor,
                                   data=data, mime=file.content_type, filename=file.filename or "", weekly=True)
    return {"draft": draft, "analysis": call(menu_review.weekly_analysis, draft) if draft["status"] != "FAILED" else None}


@weekly_router.post("/analyse")
def weekly_analyse(body: dict = Body(...), actor=Depends(reviewer)):
    draft = call(menu_review.get_draft, weekly_savoury.WEEKLY_KEY)
    if not draft or draft.get("draft_id") != body.get("draft_id") or draft.get("revision") != body.get("revision"):
        raise HTTPException(409, "Draft changed. Reload before continuing.")
    return {"analysis": call(menu_review.weekly_analysis, draft, body)}


@weekly_router.put("/draft")
def weekly_save_draft(body: dict = Body(...), actor=Depends(reviewer)):
    draft = call(menu_review.edit, weekly_savoury.WEEKLY_KEY, body.get("draft_id"),
                 body.get("revision"), body.get("rows"), actor)
    return {"draft": draft, "analysis": call(menu_review.weekly_analysis, draft, body)}


@weekly_router.post("/reject")
def weekly_reject(body: dict = Body(...), actor=Depends(reviewer)):
    return {"draft": call(menu_review.reject, weekly_savoury.WEEKLY_KEY,
                          body.get("draft_id"), body.get("revision"), actor)}


@weekly_router.post("/publish")
def weekly_publish(body: dict = Body(...), actor=Depends(reviewer)):
    draft = call(menu_review.publish_approved_menu, weekly_savoury.WEEKLY_KEY,
                 body.get("draft_id"), body.get("revision"), actor, weekly_options=body)
    return {"draft": draft, "weekly": _weekly_view()}


@weekly_router.post("/versions/{version_id}/restore")
def weekly_restore(version_id: int, actor=Depends(reviewer)):
    version = menu_review.db.get_menu_version(version_id)
    if not version or version["menu_key"] != weekly_savoury.WEEKLY_KEY or not version.get("valid_from"):
        raise HTTPException(404, "Weekly version not found.")
    return {"draft": call(menu_review.restore_as_draft, weekly_savoury.WEEKLY_KEY, version_id, actor)}


@weekly_router.post("/preview")
def weekly_preview(body: dict = Body(...), actor=Depends(reviewer)):
    """Read-only candidate preview; no conversation, send or outbound audit."""
    draft = call(menu_review.get_draft, weekly_savoury.WEEKLY_KEY)
    if not draft or draft.get("draft_id") != body.get("draft_id") or draft.get("revision") != body.get("revision"):
        raise HTTPException(409, "Draft changed. Reload before previewing.")
    analysis = call(menu_review.weekly_analysis, draft, body)
    if any(error for error in analysis["errors"] if not error.startswith(("Unusual price change", "Unusual quantity change"))):
        raise HTTPException(400, "Correct unclear menu facts before previewing.")
    rows = analysis["rows"]
    question = str(body.get("question") or "").strip().casefold()
    from camellia.rag_fallback import rag_fallback_engine
    approved_sentences = [f"{row['item']}{(' ' + row['quantity']) if row['quantity'] else ''} is ₹{row['price_inr']}."
                          for row in rows]
    if re.search(r"\b(?:available|availability|stock|ingredient|allergen|delivery|discount|payment|"
                 r"order|shelf.life|customi[sz]|event|invoice)\b", question):
        answer = "Please message our team to confirm those details."
        grounded = False
    elif re.search(r"\b(?:price|cost|how much)\b", question):
        matched = [row for row in rows if re.search(r"(?<!\w)" + re.escape(row["item"].casefold()) + r"(?!\w)", question)
                   or any(re.search(r"(?<!\w)" + re.escape(alias.casefold()) + r"(?!\w)", question)
                          for alias in row.get("aliases", []))]
        if len(matched) != 1:
            answer, grounded = "Please ask our team for the current menu price.", False
        else:
            row = matched[0]
            answer = f"{row['item']}{(' ' + row['quantity']) if row['quantity'] else ''} is ₹{row['price_inr']}."
            grounded = rag_fallback_engine.menu_grounding_report(answer, approved_sentences)[0]
    elif re.search(r"\b(?:menu|weekly|savoury)\b", question):
        menu_answer = "Current approved prices:\n" + "\n".join(approved_sentences)
        answer = menu_answer
        grounded = rag_fallback_engine.menu_grounding_report(answer, [menu_answer])[0]
    else:
        answer, grounded = "Please ask our team for the menu details you need.", False
    upcoming = analysis["valid_from"] and analysis["valid_from"] > menu_dates.today_ist().isoformat()
    return {"label": "Preview only — not sent to customers.", "answer": answer,
            "grounded": grounded, "upcoming": bool(upcoming),
            "schedule_note": "Not live yet. Scheduled for " + analysis["display_date_range"] if upcoming else None}


REVIEW_HTML = r'''
<style>
  #menuReviewPanel{max-width:850px;min-width:0;margin:0 auto}
  #menuReviewPanel [hidden]{display:none!important}
  #menuReviewPanel .mr-header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:20px}
  #menuReviewPanel .mr-header h2{margin:0;color:var(--primary)}
  #menuReviewPanel .mr-header p{margin:5px 0 0;color:var(--text-muted)}
  #menuReviewPanel .mr-catalogue-list{display:grid;gap:12px}
  #menuReviewPanel .mr-card{display:grid;grid-template-columns:minmax(0,220px) minmax(0,1fr);align-items:start;gap:12px 18px;padding:16px;background:#fff;border:1px solid var(--border);border-radius:12px;min-width:0;overflow-wrap:anywhere}
  #menuReviewPanel .mr-card-info{min-width:0}
  #menuReviewPanel .mr-card-image{aspect-ratio:4/3;min-width:0;overflow:hidden;display:grid;place-items:center;background:#f5f1ec;border:1px solid var(--border);border-radius:10px;color:var(--text-muted)}
  #menuReviewPanel .mr-card-badges{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
  #menuReviewPanel .mr-badge{font-size:.8rem;border:1px solid var(--border);border-radius:100px;padding:2px 9px;color:var(--text-muted)}
  #menuReviewPanel .mr-category-link{padding:4px 0;border:0;background:transparent;color:var(--text-muted);font-size:.85rem;cursor:pointer}
  #menuReviewPanel .mr-image-preview{display:block;width:100%;height:100%;padding:0;border:0;background:transparent;cursor:zoom-in}
  #menuReviewPanel .mr-card-image img{display:block;width:100%;height:100%;object-fit:cover}
  #menuReviewPanel .mr-image-placeholder{padding:12px;text-align:center;font-size:.9rem}
  #menuReviewPanel .mr-card-title{margin:0;font-size:1rem;color:var(--primary)}
  #menuReviewPanel .mr-card-description{margin:5px 0 0;color:var(--text-muted);font-size:.85rem}
  #menuReviewPanel .mr-publication{display:block;margin-top:6px;font-size:.75rem;color:var(--text-muted)}
  #menuReviewPanel .mr-card-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  #menuReviewPanel .mr-card-actions details{position:relative}
  #menuReviewPanel .mr-card-actions summary{list-style:none;cursor:pointer;min-width:44px;min-height:44px;display:grid;place-items:center;border:1px solid var(--border);border-radius:8px}
  #menuReviewPanel .mr-card-actions summary::-webkit-details-marker{display:none}
  #menuReviewPanel .mr-menu-actions{min-width:145px;padding:8px;background:#fff;border:1px solid var(--border);border-radius:8px}
  #menuReviewPanel .mr-menu-actions button{display:block;width:100%;min-height:44px;text-align:left}
  #menuReviewPanel .mr-inactive{font-size:.8rem;color:var(--text-muted);border:1px solid var(--border);border-radius:100px;padding:2px 9px}
  #menuReviewPanel .mr-tabs{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0;border-bottom:1px solid var(--border);padding-bottom:10px}
  #menuReviewPanel .mr-tabs button{min-height:44px}
  #menuReviewPanel .mr-tabs button[aria-selected="true"]{background:var(--primary);color:#fff}
  #menuReviewPanel .mr-current-row{display:grid;grid-template-columns:minmax(0,1fr) 90px 90px;gap:12px;padding:10px 0;border-bottom:1px solid var(--border)}
  #menuReviewPanel .mr-current-row span:last-child{text-align:right}
  #menuReviewPanel .mr-input-tabs{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
  #menuReviewPanel .mr-input-tabs button{min-height:44px}
  #menuReviewPanel .mr-drop{display:block;padding:22px;border:2px dashed var(--border);border-radius:10px;background:#fcfaf7}
  #menuReviewPanel .mr-drop{position:relative;text-align:center}
  #menuReviewPanel .mr-drop.is-dragging{border-color:var(--primary);background:#f5eef0}
  #menuReviewPanel .mr-drop:focus-within{outline:3px solid var(--primary);outline-offset:3px}
  #menuReviewPanel .mr-visually-hidden{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}
  #menuReviewPanel .mr-upload-actions{display:flex;justify-content:center;gap:8px;flex-wrap:wrap;margin-top:14px}
  #menuReviewPanel .mr-file-status{margin:12px 0 0;color:var(--text-muted);overflow-wrap:anywhere}
  #menuReviewPanel .mr-image-dialog{max-width:min(900px,calc(100vw - 24px));padding:16px}
  #menuReviewPanel .mr-image-dialog img{display:block;max-width:100%;max-height:75vh;margin:0 auto;object-fit:contain}
  #menuReviewPanel .mr-image-dialog h3{margin:0 0 12px}
  #menuReviewPanel .mr-image-dialog-actions{display:flex;justify-content:flex-end;margin-top:12px}
  #menuReviewPanel .mr-review-table{overflow-x:auto}
  #menuReviewPanel .mr-review-table table{width:100%}
  #menuReviewPanel .mr-review-table input,#menuReviewPanel .mr-review-table select{max-width:100%}
  #menuReviewPanel .mr-version{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 0;border-bottom:1px solid var(--border)}
  #menuReviewPanel dialog{max-width:min(520px,calc(100vw - 32px));max-height:calc(100vh - 32px);overflow-y:auto;margin:auto;width:100%;border:1px solid #888;border-radius:8px;padding:24px}
  #menuReviewPanel dialog::backdrop{background:#0008}
  #menuReviewPanel dialog .field-control{width:100%;margin-bottom:12px}
  #menuReviewPanel .mr-dialog-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
  @media(max-width:600px){#menuReviewPanel .mr-current-row{grid-template-columns:minmax(0,1fr) 65px 65px;font-size:.9rem}#menuReviewPanel .mr-review-table{overflow:visible}#menuReviewPanel .mr-review-table thead{display:none}#menuReviewPanel .mr-review-table tr{display:block;border:1px solid var(--border);border-radius:10px;padding:10px;margin-bottom:12px}#menuReviewPanel .mr-review-table td{display:block;border:0;padding:6px}#menuReviewPanel .mr-review-table td:before{content:attr(data-label);display:block;font-size:.75rem;color:var(--text-muted)}#menuReviewPanel .mr-review-table input,#menuReviewPanel .mr-review-table select{width:100%!important}}
  @media(max-width:600px){#menuReviewPanel .mr-card{grid-template-columns:minmax(0,1fr);gap:9px}#menuReviewPanel .mr-card-image{width:100%;max-width:360px;aspect-ratio:16/10}#menuReviewPanel .mr-card-actions{gap:8px}#menuReviewPanel .mr-menu-actions{max-width:100%}}
</style>
<section id="menuReviewPanel" aria-labelledby="menuReviewHeading">
  <select id="mrMenu" hidden aria-hidden="true"></select>
  <div id="mrCatalogueView">
    <div class="mr-header"><div><h2 id="menuReviewHeading">Menu Catalogue</h2><p>Manage your menus.</p></div>
      <button type="button" class="btn-primary" onclick="mrOpenMenuForm('add')">+ Add Menu</button></div>
    <div id="mrCatalogueList" class="mr-catalogue-list"></div>
    <button type="button" class="mr-category-link" onclick="mrManageCategories()">Manage categories</button>
    <div id="mrEmpty" hidden><p>No menus yet. Create your first menu to get started.</p>
      <button type="button" class="btn-primary" onclick="mrOpenMenuForm('add')">+ Add Menu</button></div>
  </div>
  <div id="mrSelectedView" hidden>
    <button type="button" class="btn-secondary" onclick="mrShowCatalogue()">← Menu Catalogue</button>
    <div class="mr-header" style="margin-top:16px"><div><h2 id="mrSelectedName"></h2><p id="mrSelectedDescription"></p><p id="mrSelectedStatus"></p></div><span id="mrSelectedInactive" class="mr-inactive" hidden>INACTIVE</span></div>
    <nav class="mr-tabs" aria-label="Menu sections">
      <button type="button" class="btn-secondary" data-mr-pane="current" aria-selected="true" onclick="mrShowPane('current')">Menu</button>
      <button type="button" class="btn-secondary" data-mr-pane="upload" aria-selected="false" onclick="mrShowPane('upload')">Upload Menu</button>
      <button type="button" class="btn-secondary" data-mr-pane="versions" aria-selected="false" onclick="mrShowPane('versions')">Versions</button>
    </nav>
    <div id="mrCurrentPane"><h3>Current Menu</h3><div id="mrCurrentRows"></div>
      <p id="mrLastPublished"></p><div id="mrCurrentEmpty" hidden><p>No menu published yet. Upload a menu to get started.</p>
        <button type="button" class="btn-primary" onclick="mrShowPane('upload')">Upload Menu</button></div></div>
    <div id="mrUploadPane" hidden>
      <h3>Upload Menu</h3>
      <div id="mrUploadInput">
        <div class="mr-input-tabs"><button type="button" class="btn-secondary" id="mrFileTab" onclick="mrInputMode('file')">Upload File</button>
          <button type="button" class="btn-secondary" id="mrTextTab" onclick="mrInputMode('text')">Paste Text</button></div>
        <div id="mrFilePane"><div id="mrDrop" class="mr-drop"><strong>Drop your menu here</strong><br>JPG, PNG, WEBP or PDF · Maximum 10 MB
          <div class="mr-upload-actions"><button id="mrChooseFile" type="button" class="btn-secondary" onclick="document.getElementById('mrFile').click()">Choose Menu File</button>
            <button id="mrRemoveFile" type="button" class="btn-secondary" onclick="mrClearFile()" hidden>Remove File</button></div>
          <label id="mrFileLabel" class="mr-visually-hidden" for="mrFile">Menu file to upload</label>
          <input id="mrFile" class="mr-visually-hidden" type="file" accept="image/jpeg,image/png,image/webp,application/pdf" aria-labelledby="mrFileLabel" onchange="mrFileSelected()">
          <p id="mrFileStatus" class="mr-file-status" role="status" aria-live="polite">No file selected.</p></div></div>
        <div id="mrTextPane" hidden><label for="mrText">Paste menu text</label><textarea id="mrText" class="field-control" rows="7" maxlength="50000"></textarea></div>
        <button type="button" class="btn-primary" onclick="mrExtract()" style="margin-top:16px">Continue to Review</button>
      </div>
      <div id="mrReview" hidden>
        <h3>Review Menu</h3><p id="mrReviewCount"></p><p>Check everything before publishing.</p><p id="mrStatus"></p>
        <label for="mrWeek">Printed date / week (blank if not stated)</label><input id="mrWeek" class="field-control" maxlength="160">
        <label for="mrWindow">Printed service window (blank if not stated)</label><input id="mrWindow" class="field-control" maxlength="160">
        <div style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-start">
          <div style="flex:1;min-width:0"><img id="mrSource" alt="Original menu preview (all PDF pages)" style="max-width:100%" hidden><pre id="mrOriginal" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre></div>
          <div class="mr-review-table" style="flex:2;min-width:0"><table><thead><tr><th>Item</th><th>Section</th><th>Quantity</th><th>Price (₹)</th><th>Description</th><th>Aliases</th><th>Warnings</th><th>Action</th></tr></thead><tbody id="mrRows"></tbody></table></div>
        </div>
        <div class="action-bar" id="mrActions">
          <button type="button" class="btn-secondary" onclick="mrAdd()">Add item</button>
          <button type="button" class="btn-secondary" onclick="mrSave()">Save Draft</button>
          <button type="button" class="btn-secondary" onclick="mrRetryExtraction()">Retry Extraction</button>
          <button type="button" class="btn-danger" onclick="mrReject()">Reject Draft</button>
          <button type="button" class="btn-primary" onclick="mrApprove()">Approve &amp; Publish</button>
        </div>
        <p>Leave details blank when they are not stated. Publishing text without a flyer removes the old flyer.</p>
      </div>
    </div>
    <div id="mrVersionsPane" hidden><h3>Versions</h3><div id="mrHistory"></div></div>
  </div>
  <div id="mrMessage" role="status" aria-live="polite"></div>
  <dialog id="mrCategoryDialog" aria-labelledby="mrCategoryHeading"><h3 id="mrCategoryHeading">Manage categories</h3><label for="mrCategoryChoice">Category</label><select id="mrCategoryChoice" class="field-control"></select>
    <div class="mr-card-actions"><button type="button" class="btn-secondary" onclick="mrCategoryAction('add')">+ Add category</button><button type="button" class="btn-secondary" onclick="mrCategoryAction('edit')">Edit</button><button type="button" class="btn-secondary" onclick="mrCategoryAction('delete')">Delete</button></div>
    <div class="mr-dialog-actions"><button type="button" class="btn-secondary" onclick="document.getElementById('mrCategoryDialog').close()">Done</button></div></dialog>
  <dialog id="mrMenuDialog" aria-labelledby="mrMenuDialogTitle">
    <h3 id="mrMenuDialogTitle">Add New Menu</h3>
    <form id="mrMenuForm" onsubmit="mrSaveMenu(event)">
      <label for="mrMenuName">Menu name *</label><input id="mrMenuName" class="field-control" maxlength="120" required>
      <label for="mrMenuCategory">Category *</label><select id="mrMenuCategory" class="field-control" required></select>
      <label for="mrMenuDescription">Description *</label><textarea id="mrMenuDescription" class="field-control" maxlength="1000" required></textarea>
      <label><input id="mrMenuActive" type="checkbox" checked> Active</label>
      <div class="mr-dialog-actions"><button type="button" class="btn-secondary" onclick="document.getElementById('mrMenuDialog').close()">Cancel</button><button id="mrMenuSubmit" type="submit" class="btn-primary">Add Menu</button></div>
    </form>
    <p id="mrMenuFormError" role="alert"></p>
  </dialog>
  <dialog id="mrImageDialog" class="mr-image-dialog" aria-labelledby="mrImageDialogTitle">
    <h3 id="mrImageDialogTitle"></h3><img id="mrImagePreview" alt="">
    <p id="mrImagePreviewError" role="status" hidden>This menu image could not be loaded.</p>
    <div class="mr-image-dialog-actions"><button id="mrImageDialogClose" type="button" class="btn-secondary" onclick="document.getElementById('mrImageDialog').close()">Close preview</button></div>
  </dialog>
</section>
'''

REVIEW_SCRIPT = r'''
<script>
let mrDraft=null, mrCategories=[], mrMenuCategories=[], mrCatalogue=[], mrWorkspaceRows=[], mrPreviewUrl=null, mrBusy=false, mrEditingId=null, mrInput='file', mrChosenFile=null, mrPreviewOpener=null;
function mrBase(){return '/admin/menu-manager/menus/'+encodeURIComponent(document.getElementById('mrMenu').value)+'/review';}
function mrMessage(value){document.getElementById('mrMessage').textContent=value;}
function mrWarningLabel(value){const text=String(value||'');if(/price/i.test(text))return 'Missing or unclear price';if(/quantity/i.test(text))return 'Check quantity against the source';if(/category|section/i.test(text))return 'Unknown section — choose a section';if(/duplicate|alias/i.test(text))return 'Check duplicate names or aliases';if(/read this menu|extraction|provider/i.test(text))return 'We could not read this source reliably. Please review or retry.';return 'Review required — check against the source';}
function mrFormatFileSize(bytes){const size=Number(bytes)||0;if(size<1024)return size+' B';if(size<1024*1024)return (size/1024).toFixed(1)+' KB';return (size/(1024*1024)).toFixed(1)+' MB';}
function mrUploadFileError(file){if(!file)return 'Choose a menu file first.';if(file.size>10*1024*1024)return 'This file is over 10 MB. Choose a smaller menu file.';const extension=String(file.name||'').split('.').pop().toLowerCase();const allowedTypes=['image/jpeg','image/png','image/webp','application/pdf'];const allowedExtensions=['jpg','jpeg','png','webp','pdf'];if(!allowedTypes.includes(String(file.type||'').toLowerCase())&&!allowedExtensions.includes(extension))return 'Choose a JPG, PNG, WEBP, or PDF menu file.';return '';}
function mrFileSelected(file=document.getElementById('mrFile').files?.[0]){if(!file)return false;const issue=mrUploadFileError(file);const input=document.getElementById('mrFile');const status=document.getElementById('mrFileStatus');if(issue){mrChosenFile=null;input.value='';status.textContent=issue;document.getElementById('mrRemoveFile').hidden=true;document.getElementById('mrChooseFile').textContent='Choose Menu File';return false;}mrChosenFile=file;status.textContent='Selected: '+file.name+' ('+mrFormatFileSize(file.size)+')';document.getElementById('mrRemoveFile').hidden=false;document.getElementById('mrChooseFile').textContent='Change File';mrMessage('');return true;}
function mrClearFile(){mrChosenFile=null;document.getElementById('mrFile').value='';document.getElementById('mrFileStatus').textContent='No file selected.';document.getElementById('mrRemoveFile').hidden=true;document.getElementById('mrChooseFile').textContent='Choose Menu File';}
function mrPreviewMenuImage(menu,opener){if(!menu?.image_url)return;const dialog=document.getElementById('mrImageDialog');const preview=document.getElementById('mrImagePreview');const error=document.getElementById('mrImagePreviewError');document.getElementById('mrImageDialogTitle').textContent=menu.name+' menu image';preview.hidden=false;preview.alt='Current menu image for '+menu.name;error.hidden=true;mrPreviewOpener=opener;preview.onerror=()=>{preview.hidden=true;error.hidden=false;};preview.src=menu.image_url;dialog.showModal();document.getElementById('mrImageDialogClose').focus();}
let mrHistoryPreviewUrl=null;
async function mrPreviewVersionSource(key,id,opener){try{const response=await fetch('/admin/menu-manager/menus/'+encodeURIComponent(key)+'/review/versions/'+id+'/source',{headers:getAuthHeaders()});if(!response.ok)throw Error('Source preview unavailable');const blob=await response.blob();if(mrHistoryPreviewUrl)URL.revokeObjectURL(mrHistoryPreviewUrl);mrHistoryPreviewUrl=URL.createObjectURL(blob);mrPreviewMenuImage({name:'Approved version '+id,image_url:mrHistoryPreviewUrl},opener);}catch(error){mrMessage(error.message);}}
document.getElementById('mrImageDialog').addEventListener('close',()=>{if(mrHistoryPreviewUrl){URL.revokeObjectURL(mrHistoryPreviewUrl);mrHistoryPreviewUrl=null;}const opener=mrPreviewOpener;mrPreviewOpener=null;if(opener?.isConnected)opener.focus();});
async function mrRequest(path,method='GET',body){const options={method,headers:getAuthHeaders()};if(body instanceof FormData){options.body=body;}else if(body!==undefined){options.headers={...options.headers,'Content-Type':'application/json'};options.body=JSON.stringify(body);}const response=await fetch(path,options);let data={};try{data=await response.json();}catch(_){data={};}if(!response.ok){const detail=typeof data.detail==='string'?data.detail:'';const safe=response.status<500&&detail.length<500&&!/traceback|sqlite|groq|authorization|api.key|exception|\/app\//i.test(detail);const error=Error(safe&&detail?detail:'Request could not be completed. Refresh the current state before trying again.');error.status=response.status;throw error;}return data;}
async function mrMenus(preferredId){
  try{
    const data=await mrRequest('/admin/menu-catalogue');
    mrCatalogue=(data.menus||[]).filter(menu=>!['weekly_vegetarian','weekly_non_vegetarian'].includes(menu.menu_id));mrMenuCategories=data.categories||[];
    mrWorkspaceRows=data.workspace_rows||mrCatalogue;
    const reviewBadge=document.getElementById('menuSidebarBadge');if(reviewBadge){reviewBadge.textContent=String(data.review_count||0);reviewBadge.hidden=!(data.review_count>0);}
    const picker=document.getElementById('mrMenu');picker.replaceChildren();
    const list=document.getElementById('mrCatalogueList');list.replaceChildren();
    mrCatalogue.forEach(menu=>{
      const option=document.createElement('option');option.value=menu.menu_id;option.textContent=menu.name;picker.appendChild(option);
    });
    mrWorkspaceRows.filter(menu=>menu.workspace!=='weekly').forEach(menu=>{
      const card=document.createElement('article');card.className='mr-card';
      const nameCell=document.createElement('div');nameCell.className='mr-card-info';
      const media=document.createElement('div');media.className='mr-card-image';
      if(menu.image_url){const trigger=document.createElement('button');trigger.type='button';trigger.className='mr-image-preview';trigger.setAttribute('aria-label','Preview current image for '+menu.name);trigger.onclick=()=>mrPreviewMenuImage(menu,trigger);
        const image=document.createElement('img');image.src=menu.image_url;image.alt='Current menu image for '+menu.name;image.loading='lazy';image.decoding='async';image.onerror=()=>{const placeholder=document.createElement('span');placeholder.className='mr-image-placeholder';placeholder.textContent='Menu image unavailable';placeholder.setAttribute('role','status');media.replaceChildren(placeholder);};trigger.appendChild(image);media.appendChild(trigger);
      }else{const placeholder=document.createElement('span');placeholder.className='mr-image-placeholder';placeholder.textContent='No current menu image';media.appendChild(placeholder);}
      card.appendChild(media);
      const title=document.createElement('h3');title.className='mr-card-title';title.textContent=menu.name;nameCell.appendChild(title);
      const description=document.createElement('p');description.className='mr-card-description';description.textContent=menu.description||'';nameCell.appendChild(description);card.appendChild(nameCell);
      const badges=document.createElement('div');badges.className='mr-card-badges';
      for(const value of [menu.category||'Other',menu.active?'Active':'Inactive']){const badge=document.createElement('span');badge.className='mr-badge';badge.textContent=value;badges.appendChild(badge);}nameCell.appendChild(badges);
      const lifecycle=document.createElement('small');lifecycle.className='mr-publication';lifecycle.textContent=[menu.display_status,menu.validity_label,menu.review_required?menu.review_label:null].filter(Boolean).join(' · ');nameCell.appendChild(lifecycle);
      if(menu.last_approved_date){const publication=document.createElement('small');publication.className='mr-publication';publication.textContent='Last published: '+menu.last_approved_date;nameCell.appendChild(publication);}
      const actions=document.createElement('div');actions.className='mr-card-actions';
      const open=document.createElement('button');open.type='button';open.className='btn-primary';open.textContent='Open';open.onclick=()=>menu.workspace==='weekly'?mrOpenWeekly():mrOpenMenu(menu.menu_id);actions.appendChild(open);
      if(menu.workspace!=='weekly'){
      const more=document.createElement('details');const summary=document.createElement('summary');summary.textContent='⋯';summary.setAttribute('aria-label','Actions for '+menu.name);more.appendChild(summary);
      const menuActions=document.createElement('div');menuActions.className='mr-menu-actions';
      const edit=document.createElement('button');edit.type='button';edit.className='btn-secondary';edit.textContent='Edit details';edit.onclick=()=>mrOpenMenuForm('edit',menu.menu_id);menuActions.appendChild(edit);
      const remove=document.createElement('button');remove.type='button';remove.className='btn-secondary';remove.textContent='Permanent Delete';remove.disabled=menu.actions?.delete===false;remove.title=remove.disabled?'Used by active Instagram routing':'';remove.onclick=()=>mrDeleteMenu(menu.menu_id);menuActions.appendChild(remove);
      more.appendChild(menuActions);actions.appendChild(more);
      }nameCell.appendChild(actions);list.appendChild(card);
    });
    document.getElementById('mrEmpty').hidden=!!mrCatalogue.length;
    if(preferredId && mrCatalogue.some(menu=>menu.menu_id===preferredId))await mrOpenMenu(preferredId);
    else mrShowCatalogue();
    if(typeof wsLoad==='function')await wsLoad();
    return true;
  }catch(e){mrMessage(e.message);return false;}
}
function mrShowCatalogue(){document.getElementById('menuReviewPanel').hidden=false;document.getElementById('mrCatalogueView').hidden=false;document.getElementById('mrSelectedView').hidden=true;const weekly=document.getElementById('mrWeeklyView');if(weekly)weekly.hidden=false;document.getElementById('mrMenu').value='';}
async function mrOpenWeekly(){document.getElementById('menuReviewPanel').hidden=false;document.getElementById('mrCatalogueView').hidden=true;document.getElementById('mrSelectedView').hidden=true;mrMessage('');document.getElementById('mrWeeklyView').hidden=false;document.getElementById('mrMenu').value='';if(typeof wsLoad==='function')await wsLoad();}
async function mrManageCategories(){try{const data=await mrRequest('/admin/menu-categories');igMenuCategoryRecords=data.categories||[];const picker=document.getElementById('mrCategoryChoice');picker.replaceChildren();igMenuCategoryRecords.forEach(category=>{const option=document.createElement('option');option.value=category.name;option.textContent=category.name;picker.appendChild(option);});document.getElementById('mrCategoryDialog').showModal();}catch(error){mrMessage(error.message);}}
function mrCategoryAction(action){const name=document.getElementById('mrCategoryChoice').value;document.getElementById('mrCategoryDialog').close();if(action==='delete'){document.getElementById('menuCategory').value=name;openCategoryDeleteModal();}else openCategoryModal(action==='edit'?name:'');}
async function mrOpenMenu(id,pane='current'){
  const menu=mrCatalogue.find(item=>item.menu_id===id);if(!menu)return;
  document.getElementById('mrMenu').value=id;
  document.getElementById('mrSelectedName').textContent=menu.name;
  document.getElementById('mrSelectedDescription').textContent=menu.description||'';
  const summary=mrWorkspaceRows.find(item=>item.menu_id===id);document.getElementById('mrSelectedStatus').textContent=[summary?.display_status,summary?.validity_label,summary?.review_required?summary.review_label:null].filter(Boolean).join(' · ');
  document.getElementById('mrSelectedInactive').hidden=!!menu.active;
  document.getElementById('menuReviewPanel').hidden=false;const weekly=document.getElementById('mrWeeklyView');if(weekly)weekly.hidden=true;document.getElementById('mrCatalogueView').hidden=true;document.getElementById('mrSelectedView').hidden=false;
  mrDraft=null;mrRender();await mrLoad(id);await mrShowPane(pane);
}
async function mrShowPane(pane){
  for(const [name,id] of [['current','mrCurrentPane'],['upload','mrUploadPane'],['versions','mrVersionsPane']])document.getElementById(id).hidden=name!==pane;
  document.querySelectorAll('#menuReviewPanel [data-mr-pane]').forEach(button=>button.setAttribute('aria-selected',String(button.dataset.mrPane===pane)));
  if(pane==='current')await mrLoadCurrent();if(pane==='versions')await mrHistory();if(pane==='upload')mrInputMode(mrInput);
}
function mrInputMode(mode){mrInput=mode;document.getElementById('mrFilePane').hidden=mode!=='file';document.getElementById('mrTextPane').hidden=mode!=='text';document.getElementById('mrFileTab').setAttribute('aria-selected',String(mode==='file'));document.getElementById('mrTextTab').setAttribute('aria-selected',String(mode==='text'));}
const mrDrop=document.getElementById('mrDrop');
['dragenter','dragover'].forEach(type=>mrDrop.addEventListener(type,event=>{event.preventDefault();mrDrop.classList.add('is-dragging');}));
['dragleave','dragend'].forEach(type=>mrDrop.addEventListener(type,event=>{event.preventDefault();mrDrop.classList.remove('is-dragging');}));
mrDrop.addEventListener('drop',event=>{event.preventDefault();mrDrop.classList.remove('is-dragging');if(event.dataTransfer?.files?.length){mrInputMode('file');mrFileSelected(event.dataTransfer.files[0]);}});
async function mrLoadCurrent(){
  const id=document.getElementById('mrMenu').value;if(!id)return;
  try{
    const data=await mrRequest(mrBase()+'/versions');if(document.getElementById('mrMenu').value!==id)return;
    const current=(data.versions||[]).find(version=>version.is_current&&version.approval_json);
    const rows=document.getElementById('mrCurrentRows');rows.replaceChildren();
    document.getElementById('mrCurrentEmpty').hidden=!!current;
    document.getElementById('mrLastPublished').textContent=current?'Last published: '+(current.changed_at||'Date unavailable'):'';
    if(!current)return;
    const approval=JSON.parse(current.approval_json);
    (approval.rows||[]).forEach(item=>{
      const row=document.createElement('div');row.className='mr-current-row';
      for(const value of [item.item||'',item.quantity||'',item.price_inr===null||item.price_inr===undefined?'':'₹'+item.price_inr]){
        const span=document.createElement('span');span.textContent=value;row.appendChild(span);
      }
      rows.appendChild(row);
    });
  }catch(e){mrMessage(e.message);}
}
async function mrLoad(id=document.getElementById('mrMenu').value){if(!id)return;try{const data=await mrRequest('/admin/menu-manager/menus/'+encodeURIComponent(id)+'/review');if(document.getElementById('mrMenu').value!==id)return;mrDraft=data.draft;mrCategories=data.categories;mrRender();}catch(e){mrMessage(e.message);}}
function mrOpenMenuForm(mode,id){const selected=mrCatalogue.find(m=>m.menu_id===(id||document.getElementById('mrMenu').value));if(mode==='edit'&&!selected)return;mrEditingId=mode==='edit'?selected.menu_id:null;document.getElementById('mrMenuDialogTitle').textContent=mrEditingId?'Edit Menu':'Add New Menu';document.getElementById('mrMenuSubmit').textContent=mrEditingId?'Save Changes':'Add Menu';document.getElementById('mrMenuName').value=selected&&mrEditingId?selected.name:'';document.getElementById('mrMenuDescription').value=selected&&mrEditingId?selected.description:'';document.getElementById('mrMenuActive').checked=selected&&mrEditingId?!!selected.active:true;document.getElementById('mrMenuFormError').textContent='';const category=document.getElementById('mrMenuCategory');category.replaceChildren();[...new Set([...mrMenuCategories,...(selected&&mrEditingId?[selected.category]:[])])].forEach(name=>{const option=document.createElement('option');option.value=name;option.textContent=name;category.appendChild(option);});category.value=selected&&mrEditingId?selected.category:mrMenuCategories[0]||'';document.getElementById('mrMenuDialog').showModal();document.getElementById('mrMenuName').focus();}
async function mrSaveMenu(event){event.preventDefault();if(mrBusy)return;const form=document.getElementById('mrMenuForm');if(!form.reportValidity())return;const payload={name:document.getElementById('mrMenuName').value.trim(),category:document.getElementById('mrMenuCategory').value,description:document.getElementById('mrMenuDescription').value.trim(),active:document.getElementById('mrMenuActive').checked};if(!payload.name||!payload.category||!payload.description){document.getElementById('mrMenuFormError').textContent='Complete all required fields.';return;}mrBusy=true;document.getElementById('mrMenuSubmit').disabled=true;try{let result;const editing=!!mrEditingId;if(editing){result=await mrRequest('/admin/menu-catalogue/'+encodeURIComponent(mrEditingId),'PATCH',payload);}else{const body=new FormData();Object.entries(payload).forEach(([key,value])=>body.append(key,String(value)));result=await mrRequest('/admin/menu-catalogue','POST',body);}document.getElementById('mrMenuDialog').close();await mrMenus(result.menu.menu_id);if(!editing)await mrShowPane('upload');mrMessage(editing?'Menu updated.':'Menu added. Upload or paste its content to begin review.');}catch(e){document.getElementById('mrMenuFormError').textContent=e.message;}finally{mrBusy=false;document.getElementById('mrMenuSubmit').disabled=false;}}
async function mrDeleteMenu(id){if(mrBusy)return;const menu=mrCatalogue.find(m=>m.menu_id===(id||document.getElementById('mrMenu').value));if(!menu)return;if(!confirm('Delete “'+menu.name+'”?\n\nThis permanently removes this menu and its local menu assets/history. It cannot be undone.'))return;mrBusy=true;mrMessage('Deleting selected menu…');try{await mrRequest('/admin/menu-catalogue/'+encodeURIComponent(menu.menu_id)+'/permanent','DELETE');await mrMenus();mrMessage('Menu deleted.');}catch(e){mrMessage(e.status===409?'This menu is currently being used. Remove or change the connected Instagram source first, then try again.':e.message);}finally{mrBusy=false;}}
async function mrWork(task){if(mrBusy)return;mrBusy=true;document.getElementById('mrMenu').disabled=true;try{await task();}catch(e){mrMessage(e.message);}finally{mrBusy=false;document.getElementById('mrMenu').disabled=false;}}
async function mrExtract(){return mrWork(async()=>{let data;mrMessage('Extracting menu details. The published menu is unchanged.');if(mrInput==='file'){const file=mrChosenFile||document.getElementById('mrFile').files?.[0];const issue=mrUploadFileError(file);if(issue)throw Error(issue);const form=new FormData();form.append('file',file);data=await mrRequest(mrBase()+'/extract-file','POST',form);}else{const value=document.getElementById('mrText').value.trim();if(!value)throw Error('Paste menu text first.');data=await mrRequest(mrBase()+'/extract-text','POST',{text:value});}mrDraft=data.draft;mrRender();});}
function mrRender(){
  const reviewing=!!mrDraft&&!['PUBLISHED','REJECTED'].includes(mrDraft.status);
  document.getElementById('mrReview').hidden=!reviewing;document.getElementById('mrUploadInput').hidden=reviewing;
  if(!reviewing)return;
  const done=mrDraft.status==='EXTRACTING';
  document.getElementById('mrReviewCount').textContent='We found '+mrDraft.rows.length+' items.';
  document.getElementById('mrStatus').textContent=({EXTRACTING:'Reading menu…',NEEDS_REVIEW:'Needs review',READY_TO_APPROVE:'Ready to approve',FAILED:'Needs review'})[mrDraft.status]||'Review required';
  document.getElementById('mrActions').hidden=done;
  document.getElementById('mrOriginal').textContent=mrDraft.raw_text||'';
  document.getElementById('mrWeek').value=mrDraft.menu_metadata?.week_of||'';
  document.getElementById('mrWindow').value=mrDraft.menu_metadata?.service_window||'';
  mrMessage([...new Set((mrDraft.warnings||[]).map(mrWarningLabel))].join('; '));
  const body=document.getElementById('mrRows');body.replaceChildren();
  mrDraft.rows.forEach((row,index)=>{
    const tr=document.createElement('tr');
    for(const field of ['item','category','quantity','price_inr','description','aliases']){
      const td=document.createElement('td');td.dataset.label=field==='price_inr'?'Price (₹)':field==='category'?'Section':field;
      const control=document.createElement(field==='category'?'select':'input');control.setAttribute('aria-label',field+' row '+(index+1));
      if(field==='category')for(const category of ['',...mrCategories]){const option=document.createElement('option');option.value=category;option.textContent=category||'Choose category';control.appendChild(option);}
      control.value=field==='aliases'?(row.aliases||[]).join(', '):row[field]??'';control.disabled=done;control.style.width=field==='description'?'220px':'125px';
      control.oninput=()=>{row[field]=field==='aliases'?control.value.split(',').map(value=>value.trim()).filter(Boolean):control.value;};td.appendChild(control);
      const origin=document.createElement('small');origin.textContent=row.provenance?.[field]==='OPERATOR_EDITED'?' Entered manually':' From source';td.appendChild(origin);tr.appendChild(td);
    }
    const source=document.createElement('td');source.dataset.label='Warnings';source.textContent=(row.source_page?'Page '+row.source_page+'. ':'')+[...new Set((row.warnings||[]).map(mrWarningLabel))].join('; ');tr.appendChild(source);
    const action=document.createElement('td');action.dataset.label='Action';const button=document.createElement('button');button.type='button';button.textContent='Delete Row';button.disabled=done;button.onclick=()=>{mrDraft.rows.splice(index,1);mrRender();};action.appendChild(button);tr.appendChild(action);body.appendChild(tr);
  });
  const preview=document.getElementById('mrSource');preview.hidden=true;if(mrPreviewUrl){URL.revokeObjectURL(mrPreviewUrl);mrPreviewUrl=null;}
  if(mrDraft.source_asset){const id=mrDraft.draft_id;fetch(mrBase()+'/source/'+id,{headers:getAuthHeaders()}).then(r=>{if(!r.ok)throw Error('Preview unavailable');return r.blob();}).then(blob=>{if(mrDraft?.draft_id!==id)return;mrPreviewUrl=URL.createObjectURL(blob);preview.src=mrPreviewUrl;preview.hidden=false;}).catch(e=>mrMessage(e.message));}
}
function mrAdd(){mrDraft.rows.push({item:'',category:'',quantity:'',price_inr:'',description:''});mrRender();}
async function mrSaveInternal(){const data=await mrRequest(mrBase(),'PUT',{draft_id:mrDraft.draft_id,revision:mrDraft.revision,rows:mrDraft.rows,menu_metadata:{week_of:document.getElementById('mrWeek').value,service_window:document.getElementById('mrWindow').value}});mrDraft=data.draft;mrRender();return data;}
async function mrSave(){return mrWork(async()=>{await mrSaveInternal();mrMessage('Draft saved. Published menu unchanged.');});}
async function mrRetryExtraction(){if(!mrDraft||!confirm('Read the original source again? Unsaved corrections will be replaced. The published menu will not change.'))return;return mrWork(async()=>{const data=await mrRequest(mrBase()+'/retry-extraction','POST',{draft_id:mrDraft.draft_id,revision:mrDraft.revision});mrDraft=data.draft;mrRender();mrMessage('Source read again. Check every item before approving.');});}
async function mrReject(){return mrWork(async()=>{if(!confirm('Reject this draft? The published menu will not change.'))return;const data=await mrRequest(mrBase()+'/reject','POST',{draft_id:mrDraft.draft_id,revision:mrDraft.revision});mrDraft=data.draft;mrRender();});}
async function mrApprove(){return mrWork(async()=>{await mrSaveInternal();const menu=mrCatalogue.find(item=>item.menu_id===document.getElementById('mrMenu').value);if(!confirm('Publish this menu?\n\n'+mrDraft.rows.length+' approved items will replace the current published version of '+(menu?.name||'this menu')+'.'))return;const data=await mrRequest(mrBase()+'/approve-publish','POST',{draft_id:mrDraft.draft_id,revision:mrDraft.revision});mrDraft=data.draft;mrRender();await mrShowPane('current');mrMessage('Approved and published. Menu knowledge is live.');});}
async function mrHistory(beforeId){const id=document.getElementById('mrMenu').value;if(!id)return;try{
  const data=await mrRequest(mrBase()+'/versions'+(beforeId?'?before_id='+beforeId:''));if(document.getElementById('mrMenu').value!==id)return;
  const box=document.getElementById('mrHistory');if(!beforeId)box.replaceChildren();else document.getElementById('mrMoreVersions')?.remove();
  const versions=(data.versions||[]).filter(v=>v.approval_json);if(!versions.length&&!beforeId&&!data.has_more)box.textContent='No published versions yet.';
  versions.forEach(v=>{const approval=JSON.parse(v.approval_json);const line=document.createElement('div');line.className='mr-version';
    const label=document.createElement('span');label.textContent='Version '+v.id+' · '+v.changed_at+' · Approved by '+(approval.approved_by||v.changed_by||'Admin')+' · '+(v.valid_from&&v.valid_until?v.valid_from+' – '+v.valid_until:'Standing');line.appendChild(label);
    if(approval.source_asset){const source=document.createElement('button');source.type='button';source.className='btn-secondary';source.textContent='Source flyer';source.onclick=()=>mrPreviewVersionSource(v.menu_key,v.id,source);line.appendChild(source);}
    const button=document.createElement('button');button.type='button';button.className='btn-secondary';button.textContent='Restore to review draft';button.onclick=()=>mrWork(async()=>{if(!confirm('Restore this version as a draft? It will require review and explicit approval.'))return;const data=await mrRequest(mrBase()+'/versions/'+v.id+'/restore','POST');mrDraft=data.draft;mrRender();await mrShowPane('upload');mrMessage('Version restored to a review draft. Published menu unchanged.');});line.appendChild(button);box.appendChild(line);
  });if(data.has_more){const more=document.createElement('button');more.id='mrMoreVersions';more.type='button';more.className='btn-secondary';more.textContent='Load more versions';more.onclick=()=>mrHistory(data.next_before_id);box.appendChild(more);}
}catch(e){mrMessage(e.message);}}
// All visible menu input actions converge on the review screen.
async function mmReplaceImage(key){switchTab('tab-image',null);await mrMenus();const aliases={'collections.aamar_mishti':'aamar_mishti','collections.signature_collection':'signature','weekly.vegetarian':'weekly_vegetarian','weekly.non_vegetarian':'weekly_non_vegetarian','weekly.bengali_bento':'bento'};await mrOpenMenu(aliases[key]||key,'upload');mrInputMode('file');document.getElementById('mrFile').click();}
async function submitMenuImage(){return mrExtract();}
async function mmPublishImage(){switchTab('tab-image',null);await mrMenus();mrMessage('Review the extracted facts, then use Approve & Publish.');}
</script>
'''
