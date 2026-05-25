#!/usr/bin/env python3
"""
Canvas Quiz Converter
Converts old-format QTI 1.x quiz zip files to the new Canvas
New Quizzes' QTI format is suitable for importing item banks.

Usage:
    python convert_quiz.py <input.zip> [input2.zip ...]

Output:
    Creates <input_name>_converted.zip for each input file.

No external dependencies required - uses Python standard library only.
"""

import sys
import os
import re
import copy
import uuid
import html
import zipfile
import xml.etree.ElementTree as ET
from datetime import date


# ---------------------------------------------------------------------------
# Cognero type → Canvas question_type mapping
# Only types that need an explicit question_type field injected.
# Types not listed here are kept without a question_type field (Canvas infers
# from the QTI structure, matching the reference output).
# ---------------------------------------------------------------------------
COGNERO_TYPE_CANVAS = {
    'True_False':          'true_false_question',
    'Modified_True_False': 'true_false_question',
    'Yes_No':              'multiple_choice_question',
    'Multiple_Choice':     'multiple_choice_question',
    'Multi_Mode':          'multiple_choice_question',
}


def generate_uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _indent(elem, level=0):
    """Add pretty-print indentation to an ET element tree in-place."""
    pad = '\n' + '  ' * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = pad + '  '
        if not elem.tail or not elem.tail.strip():
            elem.tail = pad
        for child in elem:
            _indent(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = pad
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = pad


def _get_cognero_type(item_elem) -> str:
    """Return the CogneroItemType string from an item's qticomment, or ''."""
    comment = item_elem.find('qticomment')
    if comment is not None and comment.text:
        text = comment.text.strip()
        if text.startswith('CogneroItemType:'):
            return text[len('CogneroItemType:'):].strip()
    return ''


def _has_question_type(item_elem) -> bool:
    """Return True if the item already has a question_type qtimetadatafield."""
    for field in item_elem.findall('.//qtimetadatafield'):
        label = field.find('fieldlabel')
        if label is not None and label.text == 'question_type':
            return True
    return False


def _inject_question_type(item_elem, canvas_type: str):
    """Inject a question_type qtimetadatafield into an item's qtimetadata."""
    qtimetadata = item_elem.find('.//itemmetadata/qtimetadata')
    if qtimetadata is None:
        return
    field = ET.Element('qtimetadatafield')
    label = ET.SubElement(field, 'fieldlabel')
    label.text = 'question_type'
    entry = ET.SubElement(field, 'fieldentry')
    entry.text = canvas_type
    qtimetadata.append(field)


def _strip_html_tags(text: str) -> str:
    """Strip HTML tags, returning plain text."""
    return re.sub(r'<[^>]+>', '', text or '').strip()


# ---------------------------------------------------------------------------
# Matching question transformation
# Cognero uses <response_grp> elements; Canvas expects <response_lid> elements
# where each lid's prompt is stored in a <material> child, all groups share
# the first group's answer idents, and resprocessing idents are normalized.
# ---------------------------------------------------------------------------

def _transform_matching(item_elem):
    """
    Transform a Cognero matching item in-place:
      response_grp elements → response_lid elements
    All response_lid groups share the first group's answer idents.
    Resprocessing varequal idents are remapped to first-group idents.
    """
    presentation = item_elem.find('.//presentation')
    if presentation is None:
        return

    grps = presentation.findall('response_grp')
    if not grps:
        return

    # Build shared answer pool from first group (ident → answer element copy)
    first_grp = grps[0]
    first_answers = first_grp.findall('.//response_label')

    # Build a mapping: old ident in group N → canonical ident from group 0
    # Idents look like I8_Q0_A2, I8_Q1_A2, etc.  Position in list is the key.
    # canonical[n] = first_answers[n].get('ident')
    canonical_idents = [a.get('ident', '') for a in first_answers]

    # Build a per-group mapping: old_ident → canonical_ident
    remap = {}
    for grp in grps:
        grp_answers = grp.findall('.//response_label')
        for idx, label in enumerate(grp_answers):
            old_id = label.get('ident', '')
            if idx < len(canonical_idents):
                remap[old_id] = canonical_idents[idx]

    # Remove all response_grp elements from presentation
    for grp in grps:
        presentation.remove(grp)

    # Get render_choice attributes from first group to reuse
    first_render = first_grp.find('render_choice')
    render_attribs = first_render.attrib if first_render is not None else {}

    # Create one response_lid per original response_grp
    for grp in grps:
        grp_id = grp.get('ident', '')
        prompt_el = grp.find('material/mattext')
        prompt_text = _strip_html_tags(prompt_el.text or '') if prompt_el is not None else ''

        lid = ET.Element('response_lid')
        lid.set('ident', grp_id)
        lid.set('rcardinality', 'Single')

        # Prompt material (plain text)
        mat = ET.SubElement(lid, 'material')
        mt = ET.SubElement(mat, 'mattext')
        mt.set('texttype', 'text/plain')
        mt.text = prompt_text

        # render_choice with flow_label and shared answer pool
        rc = ET.SubElement(lid, 'render_choice')
        for k, v in render_attribs.items():
            rc.set(k, v)
        fl = ET.SubElement(rc, 'flow_label')
        for ans in first_answers:
            new_label = ET.SubElement(fl, 'response_label')
            new_label.set('ident', ans.get('ident', ''))
            new_label.set('rshuffle', ans.get('rshuffle', 'No'))
            # Copy the mattext as plain text
            ans_mt = ans.find('.//mattext')
            if ans_mt is not None:
                ans_mat_el = ET.SubElement(new_label, 'material')
                new_mt = ET.SubElement(ans_mat_el, 'mattext')
                new_mt.set('texttype', 'text/plain')
                new_mt.text = _strip_html_tags(ans_mt.text or '')

        presentation.append(lid)

    # Fix resprocessing: remap answer idents to canonical (group-0) idents
    for varequal in item_elem.findall('.//resprocessing//varequal'):
        old_val = varequal.text or ''
        if old_val in remap:
            varequal.text = remap[old_val]


# ---------------------------------------------------------------------------
# Main XML transformation
# ---------------------------------------------------------------------------

def transform_questions_xml(raw_xml: bytes, new_assess_id: str) -> str:
    """
    Parse, modify, and re-serialize the Cognero questions.xml:
      - Inject question_type metadata for applicable Cognero types
      - Transform matching items from response_grp to response_lid
    Returns the new XML as a string.
    """
    # Strip UTF-8 BOM if present
    content = raw_xml.decode('utf-8-sig')

    # ET doesn't preserve CDATA — we accept entity-encoded output (functionally
    # equivalent for Canvas import).
    root = ET.fromstring(content)

    assessment = root.find('assessment')
    if assessment is None:
        raise ValueError("No <assessment> element in questions.xml")

    assessment.set('ident', new_assess_id)

    section = assessment.find('section')
    if section is not None:
        section.set('ident', generate_uuid())

    for item in (section if section is not None else assessment).findall('item'):
        ctype = _get_cognero_type(item)

        # Inject question_type if this Cognero type requires it and it's absent
        canvas_type = COGNERO_TYPE_CANVAS.get(ctype)
        if canvas_type and not _has_question_type(item):
            _inject_question_type(item, canvas_type)

        # Transform matching question structure
        if ctype == 'Matching':
            _transform_matching(item)

    xml_str = ET.tostring(root, encoding='unicode', xml_declaration=False)

    # Add the QTI namespace declaration that Canvas requires.
    # The Cognero source has no namespace on <questestinterop>, so we inject it.
    QTI_NS = 'http://www.imsglobal.org/xsd/ims_qtiasiv1p2'
    XSI_NS = 'http://www.w3.org/2001/XMLSchema-instance'
    xml_str = xml_str.replace(
        '<questestinterop>',
        f'<questestinterop xmlns="{QTI_NS}"'
        f' xsi:schemaLocation="{QTI_NS} http://www.imsglobal.org/xsd/ims_qtiasiv1p2p1.xsd"'
        f' xmlns:xsi="{XSI_NS}">',
        1,
    )
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str


# ---------------------------------------------------------------------------
# assessment_meta.xml
# ---------------------------------------------------------------------------

def build_assessment_meta(assess_id: str, title: str, num_questions: int) -> str:
    xml_title = html.escape(title, quote=False)
    assign_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"assignment:{assess_id}").hex
    group_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"group:{assess_id}").hex
    total = float(num_questions)

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<quiz identifier="{assess_id}" xmlns="http://canvas.instructure.com/xsd/cccv1p0" xsi:schemaLocation="http://canvas.instructure.com/xsd/cccv1p0 https://canvas.instructure.com/xsd/cccv1p0.xsd" xmlns:xsi="http://www.w3.org/2001/XmlSchema-instance">
  <title>{xml_title}</title>
  <description>
  </description>
  <shuffle_answers>false</shuffle_answers>
  <scoring_policy>keep_highest</scoring_policy>
  <hide_results>
  </hide_results>
  <quiz_type>assignment</quiz_type>
  <points_possible>{total:.0f}</points_possible>
  <require_lockdown_browser>false</require_lockdown_browser>
  <require_lockdown_browser_for_results>false</require_lockdown_browser_for_results>
  <require_lockdown_browser_monitor>false</require_lockdown_browser_monitor>
  <lockdown_browser_monitor_data />
  <show_correct_answers>true</show_correct_answers>
  <anonymous_submissions>false</anonymous_submissions>
  <could_be_locked>false</could_be_locked>
  <disable_timer_autosubmission>false</disable_timer_autosubmission>
  <allowed_attempts>1</allowed_attempts>
  <one_question_at_a_time>false</one_question_at_a_time>
  <cant_go_back>false</cant_go_back>
  <available>false</available>
  <one_time_results>false</one_time_results>
  <show_correct_answers_last_attempt>false</show_correct_answers_last_attempt>
  <only_visible_to_overrides>false</only_visible_to_overrides>
  <module_locked>false</module_locked>
  <assignment identifier="{assign_id}">
    <title>{xml_title}</title>
    <due_at />
    <lock_at />
    <unlock_at />
    <module_locked>false</module_locked>
    <workflow_state>unpublished</workflow_state>
    <assignment_overrides>
    </assignment_overrides>
    <quiz_identifierref>{assess_id}</quiz_identifierref>
    <allowed_extensions>
    </allowed_extensions>
    <has_group_category>false</has_group_category>
    <points_possible>{total:.0f}</points_possible>
    <grading_type>points</grading_type>
    <all_day>false</all_day>
    <submission_types>online_quiz</submission_types>
    <position>1</position>
    <turnitin_enabled>false</turnitin_enabled>
    <vericite_enabled>false</vericite_enabled>
    <peer_review_count>0</peer_review_count>
    <peer_reviews>false</peer_reviews>
    <automatic_peer_reviews>false</automatic_peer_reviews>
    <anonymous_peer_reviews>false</anonymous_peer_reviews>
    <grade_group_students_individually>false</grade_group_students_individually>
    <freeze_on_copy>false</freeze_on_copy>
    <omit_from_final_grade>false</omit_from_final_grade>
    <intra_group_peer_reviews>false</intra_group_peer_reviews>
    <only_visible_to_overrides>false</only_visible_to_overrides>
    <post_to_sis>false</post_to_sis>
    <moderated_grading>false</moderated_grading>
    <grader_count>0</grader_count>
    <grader_comments_visible_to_graders>true</grader_comments_visible_to_graders>
    <anonymous_grading>false</anonymous_grading>
    <graders_anonymous_to_graders>false</graders_anonymous_to_graders>
    <grader_names_visible_to_final_grader>true</grader_names_visible_to_final_grader>
    <anonymous_instructor_annotations>false</anonymous_instructor_annotations>
    <post_policy>
      <post_manually>false</post_manually>
    </post_policy>
  </assignment>
  <assignment_group_identifierref>{group_id}</assignment_group_identifierref>
  <assignment_overrides>
  </assignment_overrides>
</quiz>
'''


# ---------------------------------------------------------------------------
# imsmanifest.xml  (flat structure — all files at root)
# ---------------------------------------------------------------------------

def build_manifest(assess_id: str) -> str:
    dep_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"dependency:{assess_id}").hex
    today = date.today().isoformat()
    manifest_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"manifest:{assess_id}").hex

    return f'''<?xml version="1.0"?>
<manifest xmlns="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1" xmlns:lom="http://ltsc.ieee.org/xsd/imsccv1p1/LOM/resource" xmlns:imsmd="http://www.imsglobal.org/xsd/imsmd_v1p2" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" identifier="{manifest_id}" xsi:schemaLocation="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1 http://www.imsglobal.org/xsd/imscp_v1p1.xsd http://ltsc.ieee.org/xsd/imsccv1p1/LOM/resource http://www.imsglobal.org/profile/cc/ccv1p1/LOM/ccv1p1_lomresource_v1p0.xsd http://www.imsglobal.org/xsd/imsmd_v1p2 http://www.imsglobal.org/xsd/imsmd_v1p2p2.xsd">
  <metadata>
    <schema>IMS Content</schema>
    <schemaversion>1.1.3</schemaversion>
    <imsmd:lom>
      <imsmd:general>
        <imsmd:title>
          <imsmd:string>QTI Quiz Export</imsmd:string>
        </imsmd:title>
      </imsmd:general>
      <imsmd:lifeCycle>
        <imsmd:contribute>
          <imsmd:date>
            <imsmd:dateTime>{today}</imsmd:dateTime>
          </imsmd:date>
        </imsmd:contribute>
      </imsmd:lifeCycle>
      <imsmd:rights>
        <imsmd:copyrightAndOtherRestrictions>
          <imsmd:value>yes</imsmd:value>
        </imsmd:copyrightAndOtherRestrictions>
        <imsmd:description>
          <imsmd:string>Private (Copyrighted) - http://en.wikipedia.org/wiki/Copyright</imsmd:string>
        </imsmd:description>
      </imsmd:rights>
    </imsmd:lom>
  </metadata>
  <organizations/>
  <resources>
    <resource identifier="{assess_id}" type="imsqti_xmlv1p2">
      <file href="questions.xml"/>
      <dependency identifierref="{dep_id}"/>
    </resource>
    <resource identifier="{dep_id}" type="associatedcontent/imscc_xmlv1p1/learning-application-resource" href="assessment_meta.xml">
      <file href="assessment_meta.xml"/>
    </resource>
  </resources>
</manifest>
'''


# ---------------------------------------------------------------------------
# Main conversion pipeline
# ---------------------------------------------------------------------------

def convert_quiz_zip(input_path: str, output_path: str = None) -> str:
    """Convert a single old-format Cognero/Canvas quiz zip to new format.

    Args:
        input_path: Path to old-format zip file
        output_path: Optional output path. Defaults to <name>_converted.zip

    Returns:
        Path to the created output zip file.
    """
    input_path = os.path.abspath(input_path)

    if output_path is None:
        base = os.path.splitext(os.path.basename(input_path))[0]
        output_dir = os.path.dirname(input_path)
        output_path = os.path.join(output_dir, f"{base}_converted.zip")

    print(f"Reading: {input_path}")
    with zipfile.ZipFile(input_path, 'r') as zf:
        names = zf.namelist()
        questions_file = next((n for n in names if n.endswith('questions.xml')), None)
        if questions_file is None:
            raise FileNotFoundError(
                f"No questions.xml found in {input_path}. Files: {names}"
            )
        questions_xml_bytes = zf.read(questions_file)

    # Parse to collect title and question count for assessment_meta
    print("Parsing...")
    root = ET.fromstring(questions_xml_bytes.decode('utf-8-sig'))
    assessment = root.find('assessment')
    if assessment is None:
        raise ValueError("No <assessment> element found")
    title = assessment.get('title', 'Untitled Quiz')
    section = assessment.find('section')
    items = section.findall('item') if section is not None else []

    # Report types
    type_counts: dict = {}
    for item in items:
        ctype = _get_cognero_type(item)
        type_counts[ctype or '(unknown)'] = type_counts.get(ctype or '(unknown)', 0) + 1
    print(f"  Title: {title}")
    print(f"  Questions: {len(items)}")
    for ctype, count in sorted(type_counts.items()):
        canvas = COGNERO_TYPE_CANVAS.get(ctype, '(pass-through)')
        print(f"    {ctype}: {count}  →  {canvas}")

    assess_id = generate_uuid()

    print("Transforming questions XML...")
    new_questions_xml = transform_questions_xml(questions_xml_bytes, assess_id)

    print("Generating supporting files...")
    meta_xml = build_assessment_meta(assess_id, title, len(items))
    manifest_xml = build_manifest(assess_id)

    print(f"Writing: {output_path}")
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('questions.xml', new_questions_xml)
        zf.writestr('assessment_meta.xml', meta_xml)
        zf.writestr('imsmanifest.xml', manifest_xml)

    print(f"Done! Created {output_path}")
    return output_path


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("Error: Please provide at least one input zip file.")
        sys.exit(1)

    input_files = sys.argv[1:]

    for input_file in input_files:
        if not os.path.exists(input_file):
            print(f"Error: File not found: {input_file}")
            sys.exit(1)

        if not input_file.endswith('.zip'):
            print(f"Warning: {input_file} doesn't end in .zip, skipping.")
            continue

        try:
            convert_quiz_zip(input_file)
            print()
        except Exception as e:
            print(f"Error converting {input_file}: {e}")
            sys.exit(1)

    print("All conversions complete!")


if __name__ == '__main__':
    main()
