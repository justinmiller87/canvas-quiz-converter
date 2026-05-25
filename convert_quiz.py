#!/usr/bin/env python3
"""
Canvas Quiz Converter
Converts old-format Canvas/Cognero QTI quiz zip files to the new Canvas
New Quizzes QTI format suitable for item bank import.

Usage:
    python convert_quiz.py <input.zip> [input2.zip ...]

Output:
    Creates <input_name>_converted.zip for each input file.

No external dependencies required - uses Python standard library only.
"""

import sys
import os
import re
import uuid
import hashlib
import html
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_hex_id(seed: str) -> str:
    """Generate a deterministic 32-char hex ID from a seed string."""
    return hashlib.md5(seed.encode("utf-8")).hexdigest()


def generate_uuid() -> str:
    """Generate a random UUID v4 string."""
    return str(uuid.uuid4())


def extract_cdata_text(raw: str) -> str:
    """Extract plain text content from a CDATA-wrapped HTML string.
    
    The old format stores question/answer text inside CDATA like:
        <![CDATA[<span style="...">Question text</span>]]>
    We need to pull out the raw HTML content.
    """
    if raw is None:
        return ""
    # Strip CDATA markers if present (ElementTree usually handles this)
    text = raw.strip()
    return text


def old_html_to_new_html(raw_html: str, wrap_in_div: bool = True) -> str:
    """Convert old-format HTML (from CDATA) to new-format entity-encoded HTML.
    
    Old format: <span style="font-family: times new roman; font-size: 12pt; 
                 color: #000000; font-weight: normal; ">Text</span>
    New format: &lt;div&gt;&lt;span style="..."&gt;Text&lt;/span&gt;&lt;/div&gt;
    
    For the new format, we need to HTML-entity-encode the HTML tags and store
    them as text content of <mattext>.
    """
    if not raw_html:
        return ""
    
    text = raw_html.strip()
    
    # Clean up the old format quirks
    # Remove font-weight: normal from inline styles (new format drops it)
    text = re.sub(r'\s*font-weight:\s*normal;\s*', ' ', text)
    # Clean up double spaces in style attributes
    text = re.sub(r'\s{2,}', ' ', text)
    
    # Replace &#0039; (old format apostrophe) with actual apostrophe
    text = text.replace('&#0039;', "'")
    text = text.replace('&amp;apos;', "'")
    text = text.replace('&amp;amp;apos;', "'")
    # Replace &#0034; with actual quote
    text = text.replace('&#0034;', '"')
    
    if wrap_in_div:
        # Check if already wrapped in a div
        if not text.strip().startswith('<div'):
            text = f'<div>{text}</div>'
    
    # Entity-encode the entire HTML string
    encoded = html.escape(text, quote=True)
    
    return encoded


def old_answer_html_to_new(raw_html: str) -> str:
    """Convert old answer HTML to new format.
    
    For simple answers like "True"/"False", strip all HTML.
    For answers with formatting, entity-encode.
    """
    if not raw_html:
        return ""
    
    text = raw_html.strip()
    
    # Clean up apostrophes and quotes
    text = text.replace('&#0039;', "'")
    text = text.replace('&amp;apos;', "'")
    text = text.replace('&#0034;', '"')
    
    # Check if it's a simple text answer (no HTML tags besides span)
    stripped = re.sub(r'<[^>]+>', '', text).strip()
    
    # If it's just True/False or simple text without formatting spans
    if stripped in ('True', 'False') or '<span' not in text:
        return stripped
    
    # For formatted answers, remove font-weight: normal
    text = re.sub(r'\s*font-weight:\s*normal;\s*', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    
    # Entity-encode
    return html.escape(text, quote=True)


def determine_question_type(item_elem, ns: dict) -> str:
    """Determine question type from old-format item element."""
    # Check qticomment for Cognero type
    comment = item_elem.find('qticomment')
    if comment is not None and comment.text:
        ctype = comment.text.strip()
        if 'True_False' in ctype:
            return 'multiple_choice_question'
        elif 'Multiple_Choice' in ctype:
            return 'multiple_choice_question'
        elif 'Subjective_Short_Answer' in ctype:
            return 'essay_question'
    
    # Fallback: check for response_str (essay) vs response_lid (MC)
    flow = item_elem.find('.//flow')
    if flow is None:
        flow = item_elem.find('.//presentation')
    
    if flow is not None:
        if flow.find('.//response_str') is not None:
            return 'essay_question'
        if flow.find('.//response_lid') is not None:
            return 'multiple_choice_question'
    
    return 'multiple_choice_question'


# ---------------------------------------------------------------------------
# Question parsing (old format)
# ---------------------------------------------------------------------------

def parse_old_format(questions_xml_content: str) -> dict:
    """Parse old-format questions.xml and return structured quiz data."""
    root = ET.fromstring(questions_xml_content)
    
    assessment = root.find('assessment')
    if assessment is None:
        raise ValueError("No <assessment> element found in questions.xml")
    
    quiz_title = assessment.get('title', 'Untitled Quiz')
    
    section = assessment.find('section')
    if section is None:
        raise ValueError("No <section> element found")
    
    questions = []
    for item in section.findall('item'):
        q = parse_old_item(item)
        questions.append(q)
    
    return {
        'title': quiz_title,
        'questions': questions,
    }


def parse_old_item(item) -> dict:
    """Parse a single <item> from old format into a question dict."""
    title = item.get('title', '')
    qtype = determine_question_type(item, {})
    
    # Get question text
    mattext = item.find('.//presentation//material/mattext')
    question_html = mattext.text if mattext is not None else ''
    
    question = {
        'title': title,
        'type': qtype,
        'text_html': question_html,
        'answers': [],
        'correct_answer_index': None,
        'feedback': None,
    }
    
    if qtype == 'essay_question':
        # Extract model answer from itemfeedback
        feedback = item.find('.//itemfeedback//mattext')
        if feedback is not None:
            question['feedback'] = feedback.text
    else:
        # Parse answer choices
        response_lid = item.find('.//response_lid')
        if response_lid is not None:
            for label in response_lid.findall('.//response_label'):
                answer_id = label.get('ident', '')
                answer_mattext = label.find('.//mattext')
                answer_html = answer_mattext.text if answer_mattext is not None else ''
                question['answers'].append({
                    'old_id': answer_id,
                    'text_html': answer_html,
                })
        
        # Find correct answer
        varequal = item.find('.//resprocessing//varequal')
        if varequal is not None:
            correct_id = varequal.text.strip() if varequal.text else ''
            for i, ans in enumerate(question['answers']):
                if ans['old_id'] == correct_id:
                    question['correct_answer_index'] = i
                    break
    
    return question


# ---------------------------------------------------------------------------
# New format generation
# ---------------------------------------------------------------------------

def generate_new_format(quiz_data: dict) -> dict:
    """Generate all new-format XML file contents from parsed quiz data.
    
    Returns dict mapping filename -> content string.
    """
    title = quiz_data['title']
    questions = quiz_data['questions']
    
    # Generate assessment identifier (deterministic from title)
    assess_id = 'g' + generate_hex_id(f"assessment:{title}")[:31]
    
    # Assign new IDs to all questions and answers
    for i, q in enumerate(questions):
        q['new_id'] = generate_hex_id(f"question:{title}:{i}:{q['title']}")
        q['assess_q_ref'] = generate_hex_id(f"assess_q:{title}:{i}:{q['title']}")
        for j, ans in enumerate(q.get('answers', [])):
            ans['new_id'] = generate_uuid()
    
    # Generate files
    questions_xml = build_questions_xml(assess_id, title, questions)
    meta_xml = build_assessment_meta(assess_id, title, questions)
    manifest_xml = build_manifest(assess_id)
    
    folder = assess_id
    return {
        'imsmanifest.xml': manifest_xml,
        f'{folder}/assessment_meta.xml': meta_xml,
        f'{folder}/{assess_id}.xml': questions_xml,
    }


def build_questions_xml(assess_id: str, title: str, questions: list) -> str:
    """Build the main questions XML file in new format."""
    total_points = float(len(questions))
    
    lines = []
    lines.append('<?xml version="1.0"?>')
    lines.append('<questestinterop xmlns="http://www.imsglobal.org/xsd/ims_qtiasiv1p2"'
                 ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
                 ' xsi:schemaLocation="http://www.imsglobal.org/xsd/ims_qtiasiv1p2'
                 ' http://www.imsglobal.org/xsd/ims_qtiasiv1p2p1.xsd">')
    
    # Escape ampersands in title for XML attribute
    xml_title = html.escape(title, quote=True)
    lines.append(f'  <assessment ident="{assess_id}" title="{xml_title}">')
    lines.append('    <qtimetadata>')
    lines.append('      <qtimetadatafield>')
    lines.append('        <fieldlabel>cc_maxattempts</fieldlabel>')
    lines.append('        <fieldentry>1</fieldentry>')
    lines.append('      </qtimetadatafield>')
    lines.append('    </qtimetadata>')
    lines.append('    <section ident="root_section">')
    
    for q in questions:
        lines.extend(build_question_item(q))
    
    lines.append('    </section>')
    lines.append('  </assessment>')
    lines.append('</questestinterop>')
    lines.append('')
    
    return '\n'.join(lines)


def build_question_item(q: dict) -> list:
    """Build XML lines for a single question item in new format."""
    lines = []
    xml_title = html.escape(q['title'], quote=True)
    lines.append(f'      <item ident="{q["new_id"]}" title="{xml_title}">')
    
    # Metadata
    lines.append('        <itemmetadata>')
    lines.append('          <qtimetadata>')
    lines.append('            <qtimetadatafield>')
    lines.append('              <fieldlabel>question_type</fieldlabel>')
    lines.append(f'              <fieldentry>{q["type"]}</fieldentry>')
    lines.append('            </qtimetadatafield>')
    lines.append('            <qtimetadatafield>')
    lines.append('              <fieldlabel>points_possible</fieldlabel>')
    lines.append('              <fieldentry>1.0</fieldentry>')
    lines.append('            </qtimetadatafield>')
    
    if q['type'] != 'essay_question' and q.get('answers'):
        answer_ids = ','.join(a['new_id'] for a in q['answers'])
        lines.append('            <qtimetadatafield>')
        lines.append('              <fieldlabel>original_answer_ids</fieldlabel>')
        lines.append(f'              <fieldentry>{answer_ids}</fieldentry>')
        lines.append('            </qtimetadatafield>')
    
    lines.append('            <qtimetadatafield>')
    lines.append('              <fieldlabel>assessment_question_identifierref</fieldlabel>')
    lines.append(f'              <fieldentry>{q["assess_q_ref"]}</fieldentry>')
    lines.append('            </qtimetadatafield>')
    lines.append('            <qtimetadatafield>')
    lines.append('              <fieldlabel>calculator_type</fieldlabel>')
    lines.append('              <fieldentry>none</fieldentry>')
    lines.append('            </qtimetadatafield>')
    lines.append('          </qtimetadata>')
    lines.append('        </itemmetadata>')
    
    # Presentation
    lines.append('        <presentation>')
    
    # Question text
    q_text = old_html_to_new_html(q['text_html'], wrap_in_div=True)
    lines.append('          <material>')
    lines.append(f'            <mattext texttype="text/html">{q_text}</mattext>')
    lines.append('          </material>')
    
    if q['type'] == 'essay_question':
        lines.append('          <response_str ident="response1" rcardinality="Single">')
        lines.append('            <render_fib>')
        lines.append('              <response_label ident="answer1" rshuffle="No"/>')
        lines.append('            </render_fib>')
        lines.append('          </response_str>')
    else:
        lines.append('          <response_lid ident="response1" rcardinality="Single">')
        lines.append('            <render_choice>')
        for ans in q.get('answers', []):
            a_text = old_answer_html_to_new(ans['text_html'])
            lines.append(f'              <response_label ident="{ans["new_id"]}">')
            lines.append('                <material>')
            lines.append(f'                  <mattext texttype="text/html">{a_text}</mattext>')
            lines.append('                </material>')
            lines.append('              </response_label>')
        lines.append('            </render_choice>')
        lines.append('          </response_lid>')
    
    lines.append('        </presentation>')
    
    # Response processing
    lines.append('        <resprocessing>')
    lines.append('          <outcomes>')
    
    if q['type'] == 'essay_question':
        lines.append('            <decvar maxvalue="100" minvalue="0" varname="SCORE" vartype="Decimal"/>')
        lines.append('          </outcomes>')
    else:
        lines.append('            <decvar maxvalue="100" minvalue="0" varname="SCORE" vartype="Decimal"/>')
        lines.append('          </outcomes>')
        
        if q['correct_answer_index'] is not None and q.get('answers'):
            correct_id = q['answers'][q['correct_answer_index']]['new_id']
            lines.append('          <respcondition continue="No">')
            lines.append('            <conditionvar>')
            lines.append(f'              <varequal respident="response1">{correct_id}</varequal>')
            lines.append('            </conditionvar>')
            lines.append('            <setvar action="Set" varname="SCORE">100</setvar>')
            lines.append('          </respcondition>')
    
    lines.append('        </resprocessing>')
    lines.append('      </item>')
    
    return lines


def build_assessment_meta(assess_id: str, title: str, questions: list) -> str:
    """Build the assessment_meta.xml file."""
    total_points = float(len(questions))
    xml_title = html.escape(title, quote=True)
    assign_id = generate_hex_id(f"assignment:{title}")
    group_id = 'g' + generate_hex_id(f"group:{title}")[:31]
    
    return f'''<?xml version="1.0"?>
<quiz xmlns="http://canvas.instructure.com/xsd/cccv1p0" xmlns:xsi="http://canvas.instructure.com/xsd/cccv1p0 https://canvas.instructure.com/xsd/cccv1p0.xsd" identifier="{assess_id}">
  <title>{xml_title}</title>
  <description/>
  <due_at/>
  <lock_at/>
  <unlock_at/>
  <shuffle_questions>false</shuffle_questions>
  <shuffle_answers>false</shuffle_answers>
  <calculator_type>none</calculator_type>
  <scoring_policy>keep_highest</scoring_policy>
  <hide_results/>
  <quiz_type>assignment</quiz_type>
  <points_possible>{total_points:.1f}</points_possible>
  <require_lockdown_browser>false</require_lockdown_browser>
  <require_lockdown_browser_for_results>false</require_lockdown_browser_for_results>
  <require_lockdown_browser_monitor>false</require_lockdown_browser_monitor>
  <lockdown_browser_monitor_data/>
  <show_correct_answers>false</show_correct_answers>
  <anonymous_submissions>false</anonymous_submissions>
  <could_be_locked>false</could_be_locked>
  <disable_timer_autosubmission>false</disable_timer_autosubmission>
  <allowed_attempts>1</allowed_attempts>
  <build_on_last_attempt>false</build_on_last_attempt>
  <one_question_at_a_time>false</one_question_at_a_time>
  <cant_go_back>false</cant_go_back>
  <available>false</available>
  <one_time_results>false</one_time_results>
  <show_correct_answers_last_attempt>false</show_correct_answers_last_attempt>
  <only_visible_to_overrides>false</only_visible_to_overrides>
  <module_locked>false</module_locked>
  <allow_clear_mc_selection>false</allow_clear_mc_selection>
  <disable_document_access>false</disable_document_access>
  <result_view_restricted>true</result_view_restricted>
  <display_items>true</display_items>
  <display_item_feedback>true</display_item_feedback>
  <display_item_response>true</display_item_response>
  <display_points_awarded>true</display_points_awarded>
  <display_points_possible>true</display_points_possible>
  <display_item_correct_answer>true</display_item_correct_answer>
  <display_item_response_correctness>true</display_item_response_correctness>
  <display_item_response_qualifier>always</display_item_response_qualifier>
  <show_item_responses_at/>
  <hide_item_responses_at/>
  <display_item_response_correctness_qualifier>always</display_item_response_correctness_qualifier>
  <show_item_response_correctness_at/>
  <hide_item_response_correctness_at/>
  <assignment identifier="{assign_id}">
    <title>{xml_title}</title>
    <due_at/>
    <lock_at/>
    <unlock_at/>
    <module_locked>false</module_locked>
    <workflow_state>unpublished</workflow_state>
    <assignment_overrides/>
    <assignment_overrides/>
    <quiz_identifierref>{assess_id}</quiz_identifierref>
    <allowed_extensions/>
    <has_group_category>false</has_group_category>
    <points_possible>{total_points:.1f}</points_possible>
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
    <assignment_group_identifierref>{group_id}</assignment_group_identifierref>
    <assignment_overrides/>
  </assignment>
</quiz>
'''


def build_manifest(assess_id: str) -> str:
    """Build the imsmanifest.xml file."""
    dep_id = generate_hex_id(f"dependency:{assess_id}")
    today = date.today().isoformat()
    
    return f'''<?xml version="1.0"?>
<manifest xmlns="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1" xmlns:lom="http://ltsc.ieee.org/xsd/imsccv1p1/LOM/resource" xmlns:imsmd="http://www.imsglobal.org/xsd/imsmd_v1p2" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" identifier="{generate_hex_id(f"manifest:{assess_id}")}" xsi:schemaLocation="http://www.imsglobal.org/xsd/imsccv1p1/imscp_v1p1 http://www.imsglobal.org/xsd/imscp_v1p1.xsd http://ltsc.ieee.org/xsd/imsccv1p1/LOM/resource http://www.imsglobal.org/profile/cc/ccv1p1/LOM/ccv1p1_lomresource_v1p0.xsd http://www.imsglobal.org/xsd/imsmd_v1p2 http://www.imsglobal.org/xsd/imsmd_v1p2p2.xsd">
  <metadata>
    <schema>IMS Content</schema>
    <schemaversion>1.1.3</schemaversion>
    <imsmd:lom>
      <imsmd:general>
        <imsmd:title>
          <imsmd:string>QTI Quiz Export for </imsmd:string>
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
      <file href="{assess_id}/{assess_id}.xml"/>
      <dependency identifierref="{dep_id}"/>
    </resource>
    <resource identifier="{dep_id}" type="associatedcontent/imscc_xmlv1p1/learning-application-resource" href="{assess_id}/assessment_meta.xml">
      <file href="{assess_id}/assessment_meta.xml"/>
    </resource>
  </resources>
</manifest>
'''


# ---------------------------------------------------------------------------
# Main conversion pipeline
# ---------------------------------------------------------------------------

def convert_quiz_zip(input_path: str, output_path: str = None) -> str:
    """Convert a single old-format quiz zip to new format.
    
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
    
    # Read the input zip
    print(f"Reading: {input_path}")
    with zipfile.ZipFile(input_path, 'r') as zf:
        names = zf.namelist()
        
        # Find questions.xml (may be at root or in a subdirectory)
        questions_file = None
        for name in names:
            if name.endswith('questions.xml'):
                questions_file = name
                break
        
        if questions_file is None:
            raise FileNotFoundError(
                f"No questions.xml found in {input_path}. "
                f"Files in zip: {names}"
            )
        
        questions_xml = zf.read(questions_file).decode('utf-8')
    
    # Parse old format
    print("Parsing old format...")
    quiz_data = parse_old_format(questions_xml)
    
    q_count = len(quiz_data['questions'])
    q_types = {}
    for q in quiz_data['questions']:
        q_types[q['type']] = q_types.get(q['type'], 0) + 1
    
    print(f"  Title: {quiz_data['title']}")
    print(f"  Questions: {q_count}")
    for qtype, count in sorted(q_types.items()):
        print(f"    {qtype}: {count}")
    
    # Generate new format
    print("Generating new format...")
    new_files = generate_new_format(quiz_data)
    
    # Write output zip
    print(f"Writing: {output_path}")
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for filename, content in new_files.items():
            zf.writestr(filename, content)
    
    print(f"Done! Created {output_path}")
    print(f"  Files in output: {list(new_files.keys())}")
    
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
