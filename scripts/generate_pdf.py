import os
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY

# Colours
BROWN      = HexColor("#5C3317")
LIGHT_TAN  = HexColor("#F5EFE6")
CREAM      = HexColor("#FBF7F2")
GREEN_DARK = HexColor("#1B4332")
GREEN_LITE = HexColor("#D8F3DC")
RED_DARK   = HexColor("#7B1D1D")
RED_LITE   = HexColor("#FDDEDE")
GREY_TEXT  = HexColor("#4A4A4A")
BORDER     = HexColor("#C8B49A")

pdf_path = os.path.join(os.getcwd(), "Why_Not_RAG_Camellia.pdf")

doc = SimpleDocTemplate(
    pdf_path,
    pagesize=A4,
    rightMargin=2.2*cm, leftMargin=2.2*cm,
    topMargin=2*cm, bottomMargin=2*cm
)

styles = getSampleStyleSheet()

# Custom styles
def S(name, **kw):
    return ParagraphStyle(name, **kw)

title_style = S("Title2",
    fontName="Helvetica-Bold", fontSize=22,
    textColor=BROWN, alignment=TA_CENTER, spaceAfter=4)

subtitle_style = S("Sub",
    fontName="Helvetica", fontSize=11,
    textColor=GREY_TEXT, alignment=TA_CENTER, spaceAfter=16)

h1 = S("H1",
    fontName="Helvetica-Bold", fontSize=14,
    textColor=BROWN, spaceBefore=18, spaceAfter=6)

h2 = S("H2",
    fontName="Helvetica-Bold", fontSize=11,
    textColor=GREY_TEXT, spaceBefore=10, spaceAfter=4)

body = S("Body2",
    fontName="Helvetica", fontSize=10,
    textColor=GREY_TEXT, leading=16,
    alignment=TA_JUSTIFY, spaceAfter=8)

bullet = S("Bullet2",
    fontName="Helvetica", fontSize=10,
    textColor=GREY_TEXT, leading=16,
    leftIndent=16, spaceAfter=4,
    alignment=TA_LEFT)

bold_body = S("BoldBody",
    fontName="Helvetica-Bold", fontSize=10,
    textColor=GREY_TEXT, leading=16, spaceAfter=4)

label_style = S("Label",
    fontName="Helvetica-Bold", fontSize=9,
    textColor=white, alignment=TA_CENTER)

note_style = S("Note",
    fontName="Helvetica-Oblique", fontSize=9,
    textColor=BROWN, leading=14,
    leftIndent=12, spaceAfter=6)

story = []

# ── HEADER ──────────────────────────────────────────────────────────────────
story.append(Spacer(1, 0.4*cm))
story.append(Paragraph("camellia.artisanal", subtitle_style))
story.append(Paragraph("WhatsApp &amp; Instagram Chatbot", subtitle_style))
story.append(HRFlowable(width="100%", thickness=1.5, color=BROWN))
story.append(Spacer(1, 0.3*cm))
story.append(Paragraph("Why We Don't Use RAG", title_style))
story.append(Paragraph(
    "A plain-language explanation of the architecture decision, "
    "with a full comparison of RAG vs Deterministic Matching",
    subtitle_style))
story.append(HRFlowable(width="100%", thickness=1, color=BORDER))
story.append(Spacer(1, 0.5*cm))

# ── SECTION 1 ────────────────────────────────────────────────────────────────
story.append(Paragraph("1. What is RAG?", h1))
story.append(Paragraph(
    "RAG stands for <b>Retrieval-Augmented Generation</b>. It is a technique where "
    "an AI model is given a set of documents or data as a reference, and then "
    "<i>generates</i> a fresh sentence as its answer each time a customer asks a question.",
    body))
story.append(Paragraph(
    "Even with strict grounding instructions, the model still <b>writes new words</b> "
    "on every reply. It does not copy a fixed sentence — it composes one. "
    "This is the key difference from what we built.", body))

# ── SECTION 2 ────────────────────────────────────────────────────────────────
story.append(Paragraph("2. What We Built Instead", h1))
story.append(Paragraph(
    "The Camellia bot uses <b>Deterministic Template Matching</b>. "
    "Every possible answer is written in advance — by you, sourced from the client "
    "(Somdeb Ji) directly — and stored in <b>qa_templates.json</b>. "
    "The bot does not generate text. It only looks up and returns the pre-approved sentence.",
    body))
story.append(Paragraph(
    "If a customer's message matches a known trigger keyword, "
    "the exact approved answer is returned. "
    "If nothing matches, the message is forwarded to a human. <b>No exceptions.</b>", body))

# Comparison diagram as a simple table
story.append(Spacer(1, 0.3*cm))

compare_data = [
    [Paragraph("<b>Step</b>", label_style),
     Paragraph("<b>RAG System</b>", label_style),
     Paragraph("<b>Camellia Bot (Deterministic)</b>", label_style)],
    ["1", "Customer sends message", "Customer sends message"],
    ["2", "System retrieves related documents", "System checks trigger keywords"],
    ["3", "AI model reads documents + GENERATES new sentence", "Bot returns the EXACT pre-approved sentence"],
    ["4", "Customer sees AI-written reply", "Customer sees client-approved reply"],
    ["5", "Slight risk of added/changed words", "Zero risk — same text every time"],
]

compare_style = TableStyle([
    ("BACKGROUND",   (0,0), (-1,0), BROWN),
    ("TEXTCOLOR",    (0,0), (-1,0), white),
    ("FONTNAME",     (0,0), (-1,0), "Helvetica-Bold"),
    ("FONTSIZE",     (0,0), (-1,-1), 9),
    ("BACKGROUND",   (0,1), (-1,-1), CREAM),
    ("ROWBACKGROUNDS",(0,1), (-1,-1), [CREAM, LIGHT_TAN]),
    ("ALIGN",        (0,0), (0,-1), "CENTER"),
    ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
    ("GRID",         (0,0), (-1,-1), 0.5, BORDER),
    ("LEFTPADDING",  (0,0), (-1,-1), 8),
    ("RIGHTPADDING", (0,0), (-1,-1), 8),
    ("TOPPADDING",   (0,0), (-1,-1), 6),
    ("BOTTOMPADDING",(0,0), (-1,-1), 6),
])

compare_table = Table(compare_data, colWidths=[1.2*cm, 6*cm, 8.3*cm])
compare_table.setStyle(compare_style)
story.append(compare_table)
story.append(Spacer(1, 0.5*cm))

# ── SECTION 3 ────────────────────────────────────────────────────────────────
story.append(Paragraph("3. Why RAG Cannot Be Made Safe Enough For This Use Case", h1))
story.append(Paragraph(
    "The client's requirement (from the Minutes of Meeting) was explicit: "
    "<i>\"The bot can answer only if the question is in syllabus — pre-defined by us. "
    "Else forward to us.\"</i>", body))
story.append(Paragraph(
    "RAG cannot meet this requirement, no matter how strict the instructions, because:", body))

reasons = [
    ("<b>Generation always introduces risk.</b> Even the best RAG systems with tight "
     "grounding occasionally add a word, reframe a sentence, or infer a detail that "
     "was not in the source. This is not a bug — it is how language models work."),
    ("<b>\"Low chance of error\" is not zero.</b> At 500 messages per day, a 0.1% "
     "error rate means a wrong answer every 5 days. Over a year, that is 70+ customers "
     "receiving incorrect information — about price, allergens, or delivery."),
    ("<b>Allergen and food-safety information cannot tolerate any drift.</b> "
     "The bot answers questions about nut content and Halal status. A single "
     "AI-generated reassurance that turns out to be wrong is a real safety risk, "
     "not just a bad experience."),
    ("<b>Price accuracy is financial.</b> If the bot says a sondesh box is Rs 400 "
     "when it is Rs 450, the client either loses money or loses customer trust. "
     "RAG can drift on numbers even with retrieval grounding."),
    ("<b>We proved this happened.</b> During development, the agent used a RAG-like "
     "generation approach and invented: a fake email address, fake Jolbhora pricing "
     "(10pc/Rs 450 — does not exist), a fake staff person's name, fake storage "
     "instructions, and fake allergen guarantees. Every single one was caught only "
     "because a human reviewer compared it against the client's original wording. "
     "RAG in production has no such reviewer."),
]

for r in reasons:
    story.append(Paragraph(f"&bull;  {r}", bullet))

story.append(Spacer(1, 0.3*cm))

# ── SECTION 4 ── PROS AND CONS TABLE ─────────────────────────────────────────
story.append(Paragraph("4. Full Pros and Cons Comparison", h1))

def pro(text):
    return Paragraph(f"<font color='#1B4332'>+ {text}</font>",
                     S("p", fontName="Helvetica", fontSize=9,
                       textColor=GREEN_DARK, leading=14, leftIndent=4))

def con(text):
    return Paragraph(f"<font color='#7B1D1D'>- {text}</font>",
                     S("c", fontName="Helvetica", fontSize=9,
                       textColor=RED_DARK, leading=14, leftIndent=4))

def neutral(text):
    return Paragraph(text,
                     S("n", fontName="Helvetica", fontSize=9,
                       textColor=GREY_TEXT, leading=14, leftIndent=4))

pc_header = [
    Paragraph("<b>Dimension</b>", label_style),
    Paragraph("<b>RAG</b>", label_style),
    Paragraph("<b>Deterministic Matching (Camellia)</b>", label_style),
]

pc_data = [
    pc_header,
    [neutral("Answer accuracy"),
     con("High most of the time, but never 100%"),
     pro("100% — same answer every time, no exceptions")],
    [neutral("Allergen / safety risk"),
     con("Can generate plausible but incorrect claims"),
     pro("Impossible to add unchecked claims")],
    [neutral("Price accuracy"),
     con("Can drift on numbers under paraphrase"),
     pro("Exact figure from menu.json, always")],
    [neutral("New menu each week"),
     con("Must re-index/re-embed data and re-test"),
     pro("Replace one JSON block, done in seconds")],
    [neutral("Client control"),
     con("Client cannot audit what AI will say next"),
     pro("Client approves every possible reply in advance")],
    [neutral("API cost per message"),
     con("Yes — every customer message costs money"),
     pro("Zero — no API call per reply")],
    [neutral("Setup complexity"),
     con("Embedding pipeline, vector DB, RAG chain, prompts"),
     pro("JSON file + keyword list")],
    [neutral("Response speed"),
     con("Network call to AI API on every message"),
     pro("In-memory lookup, near-instant")],
    [neutral("Works when internet is down"),
     con("No — needs API connection"),
     pro("Yes — fully local")],
    [neutral("Handles unseen questions"),
     pro("Yes — generates a plausible answer"),
     neutral("No — escalates to human (this is by design)")],
    [neutral("Scales to large catalogs"),
     pro("Yes — handles thousands of products well"),
     con("Trigger list grows with catalog size")],
    [neutral("Multilingual / Hinglish"),
     pro("Handles naturally via embeddings"),
     neutral("Requires manual trigger phrases per language")],
]

pc_table_style = TableStyle([
    ("BACKGROUND",    (0,0), (-1,0), BROWN),
    ("TEXTCOLOR",     (0,0), (-1,0), white),
    ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
    ("FONTSIZE",      (0,0), (-1,-1), 9),
    ("ROWBACKGROUNDS",(0,1), (-1,-1), [CREAM, LIGHT_TAN]),
    ("ALIGN",         (0,0), (0,-1), "LEFT"),
    ("VALIGN",        (0,0), (-1,-1), "TOP"),
    ("GRID",          (0,0), (-1,-1), 0.5, BORDER),
    ("LEFTPADDING",   (0,0), (-1,-1), 8),
    ("RIGHTPADDING",  (0,0), (-1,-1), 8),
    ("TOPPADDING",    (0,0), (-1,-1), 6),
    ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ("BACKGROUND",    (0,10), (-1,10), HexColor("#FFF3CD")),
    ("BACKGROUND",    (0,11), (-1,11), HexColor("#FFF3CD")),
    ("BACKGROUND",    (0,12), (-1,12), HexColor("#FFF3CD")),
])

pc_table = Table(pc_data, colWidths=[4*cm, 5.5*cm, 6*cm])
pc_table.setStyle(pc_table_style)
story.append(pc_table)

story.append(Spacer(1, 0.25*cm))
story.append(Paragraph(
    "* Yellow rows = areas where RAG has a genuine advantage, "
    "but none of them apply to Camellia's current catalog size or use case.",
    note_style))

# ── SECTION 5 ────────────────────────────────────────────────────────────────
story.append(Paragraph("5. The Real Solution for Weekly Menu Updates", h1))
story.append(Paragraph(
    "The most common argument for RAG is: <i>\"the menu changes every week, "
    "we can't manually update it.\"</i> This is already solved without RAG:", body))

sol_data = [
    [Paragraph("<b>Method</b>", label_style),
     Paragraph("<b>How It Works</b>", label_style),
     Paragraph("<b>Client Effort</b>", label_style)],
    ["Photo Upload",
     "Client uploads new menu photo to /admin portal. OCR reads it. menu.json updates automatically.",
     "Send one photo"],
    ["Google Sheet Sync",
     "Client edits a Google Sheet. Cron job runs every Thursday night. menu.json updates automatically.",
     "Edit one Sheet"],
    ["Admin Portal",
     "Client logs in, edits fields directly, saves.",
     "2 minutes of typing"],
]

sol_style = TableStyle([
    ("BACKGROUND",    (0,0), (-1,0), BROWN),
    ("TEXTCOLOR",     (0,0), (-1,0), white),
    ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
    ("FONTSIZE",      (0,0), (-1,-1), 9),
    ("ROWBACKGROUNDS",(0,1), (-1,-1), [CREAM, LIGHT_TAN]),
    ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
    ("GRID",          (0,0), (-1,-1), 0.5, BORDER),
    ("LEFTPADDING",   (0,0), (-1,-1), 8),
    ("RIGHTPADDING",  (0,0), (-1,-1), 8),
    ("TOPPADDING",    (0,0), (-1,-1), 6),
    ("BOTTOMPADDING", (0,0), (-1,-1), 6),
])

sol_table = Table(sol_data, colWidths=[3.5*cm, 8.5*cm, 3.5*cm])
sol_table.setStyle(sol_style)
story.append(sol_table)
story.append(Spacer(1, 0.3*cm))
story.append(Paragraph(
    "In all three cases: the bot's answer templates never change. "
    "Only the data they point to changes. "
    "No re-indexing, no re-embedding, no re-testing of AI outputs.", body))

# ── SECTION 6 ────────────────────────────────────────────────────────────────
story.append(Paragraph("6. When Would RAG Actually Make Sense?", h1))
story.append(Paragraph(
    "RAG is the right tool when all of the following are true:", body))

rag_yes = [
    "The product catalog has hundreds or thousands of items (not ~40)",
    "Products are described in unstructured natural language, not clean JSON",
    "Exact wording of answers does not need to be client-approved",
    "Allergen, safety, or financial accuracy is not critical",
    "The team has budget for ongoing API costs per message",
]
for y in rag_yes:
    story.append(Paragraph(f"&bull;  {y}", bullet))

story.append(Paragraph(
    "None of these conditions apply to Camellia Artisanal right now. "
    "If the business grows to hundreds of products, or expands to a website with "
    "open-ended queries, RAG could be reconsidered at that stage.", body))

# ── SECTION 7 ── VERDICT ─────────────────────────────────────────────────────
story.append(Paragraph("7. Summary", h1))

verdict_data = [
    [Paragraph(
        "The Camellia bot does not use RAG because the client's requirement is "
        "<b>\"in syllabus only, else forward to human.\"</b> "
        "RAG cannot guarantee \"in syllabus only\" — it can only approximate it. "
        "Deterministic template matching can guarantee it, by design, "
        "with zero ongoing cost and zero risk of AI-generated wrong answers "
        "about allergens, prices, or kitchen policies.",
        S("verd", fontName="Helvetica", fontSize=10,
          textColor=BROWN, leading=16, leftIndent=8, rightIndent=8)
    )]
]
verdict_table = Table(verdict_data, colWidths=[15.5*cm])
verdict_table.setStyle(TableStyle([
    ("BACKGROUND",    (0,0), (-1,-1), LIGHT_TAN),
    ("BOX",           (0,0), (-1,-1), 1.5, BROWN),
    ("TOPPADDING",    (0,0), (-1,-1), 12),
    ("BOTTOMPADDING", (0,0), (-1,-1), 12),
    ("LEFTPADDING",   (0,0), (-1,-1), 12),
    ("RIGHTPADDING",  (0,0), (-1,-1), 12),
]))
story.append(verdict_table)

story.append(Spacer(1, 0.6*cm))
story.append(HRFlowable(width="100%", thickness=1, color=BORDER))
story.append(Spacer(1, 0.2*cm))
story.append(Paragraph(
    "Prepared for camellia.artisanal | Chatbot Architecture Documentation | 2026",
    S("footer", fontName="Helvetica", fontSize=8,
      textColor=HexColor("#999999"), alignment=TA_CENTER)))

doc.build(story)
print(f"PDF generated successfully at {pdf_path}")
