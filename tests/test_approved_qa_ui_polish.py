from camellia.admin_approved_qa import HTML, SCRIPT


def test_edit_dialog_is_explicitly_centered():
    assert 'id="aqDialog"' in HTML
    assert 'position:fixed' in HTML
    assert 'left:50%' in HTML
    assert 'top:50%' in HTML
    assert 'transform:translate(-50%,-50%)' in HTML


def test_question_variations_are_visible_not_just_counted():
    assert 'accepted question variation' in SCRIPT
    assert 'click to view' in SCRIPT
    assert 'Built-in question variations (read-only)' in SCRIPT
    assert 'Additional Admin question variations (one per line)' in SCRIPT
    assert 'aqVariationList' in SCRIPT


def test_changed_form_publish_refreshes_preview_instead_of_silent_noop():
    assert "function aqMarkDirty()" in SCRIPT
    assert "Preview Required Before Publish" in SCRIPT
    assert "if(!aqToken){await aqPreview();return;}" in SCRIPT
    assert "Preview ready." in SCRIPT
    assert "Changes detected." in SCRIPT


def test_validity_display_includes_seconds_and_ist_label():
    assert "second:'2-digit'" in SCRIPT
    assert "+' IST'" in SCRIPT
