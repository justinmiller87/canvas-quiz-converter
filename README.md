# Canvas Quiz Converter

Convert old-format Canvas QTI 1.x quiz exports directly into the new Canvas **New Quizzes** format — ready for item bank import — without ever touching the Canvas UI.

## The Problem

If you have quiz files exported from QTI v1.2 sources, importing them into Canvas New Quizzes item banks requires a tedious multi-step process:

1. Import the old quiz zip into a Canvas course
2. Navigate to the quizzes section
3. Migrate the quiz to the New Quizzes format
4. Open the migrated quiz
5. Export the new quiz as a zip file
6. Create a new item bank
7. Import the exported zip into the item bank
8. Delete both the original and migrated quizzes from the course
9. Build your new quiz from the item bank

**This tool eliminates steps 1–5 and 8**, converting the file format offline so you can skip straight to importing into an item bank.

## Your New Workflow

1. `python3 convert_quiz.py <quiz.zip>`
2. Create a new item bank in Canvas
3. Import the `_converted.zip` file into the item bank
4. Build your quiz from the item bank

## Requirements

- **Python 3.6+**
- No external dependencies (uses only the Python standard library)

## Usage

### Single file

```bash
python3 convert_quiz.py Chapter_01__An_Overview_of_Ethics.zip
```

### Multiple files

```bash
python3 convert_quiz.py Chapter_01.zip Chapter_02.zip Chapter_03.zip
```

### Batch convert all zips in a directory

```bash
python3 convert_quiz.py *.zip
```

### Output

For each input file, a converted file is created in the same directory with `_converted` appended:

```
Chapter_01__An_Overview_of_Ethics.zip
  → Chapter_01__An_Overview_of_Ethics_converted.zip
```

## Supported Question Types

The following question types are handled with this script. Types marked with a note are passed through to Canvas as-is (Canvas infers the type from the QTI structure); types with an explicit Canvas type have that field injected into the metadata.

| Cognero Type | Canvas New Quizzes Type | Notes |
|---|---|---|
| True/False | `true_false_question` | ✅ |
| Modified True/False | `true_false_question` | ✅ |
| Yes/No | `multiple_choice_question` | ✅ |
| Multiple Choice | `multiple_choice_question` | ✅ |
| Multiple Response | `multiple_answers_question` | ✅ Inferred from `rcardinality="Multiple"` |
| Numeric Response | Short answer | ✅ Correct answer preserved |
| Completion | Short answer | ✅ Correct answer preserved |
| Multi-Blank | `fill_in_multiple_blanks_question` | ✅ Pass-through |
| Matching | `matching_question` | ✅ Transformed to Canvas `response_lid` format |
| Objective Short Answer | Short answer | ✅ Correct answer preserved |
| Subjective Short Answer | Essay | ✅ |
| Multi-Mode | `multiple_choice_question` | ✅ |
| Ordering | Ordering | ✅ Pass-through |
| Opinion Scale/Likert | Multiple choice | ✅ Pass-through |
| Essay | Essay | ✅ |

## Example Output

```
$ python3 convert_quiz.py Chapter_03__Cyberattacks_and_Cybersecurity.zip

Reading: /path/to/Chapter_03__Cyberattacks_and_Cybersecurity.zip
Parsing...
  Title: Chapter 03: Cyberattacks and Cybersecurity
  Questions: 60
    Essay: 5  →  (pass-through)
    Multiple_Choice: 50  →  multiple_choice_question
    True_False: 5  →  true_false_question
Transforming questions XML...
Generating supporting files...
Writing: Chapter_03__Cyberattacks_and_Cybersecurity_converted.zip
Done! Created Chapter_03__Cyberattacks_and_Cybersecurity_converted.zip

All conversions complete!
```

## How It Works

The converter uses a **pass-through approach**: the original Cognero QTI XML is preserved almost entirely, with only the minimum changes needed for Canvas to accept the import. This keeps the question text, answer choices, correct answers, scoring logic, and all metadata intact.

The specific changes made are:

1. **question_type injection** — A `question_type` qtimetadata field is added to items whose Cognero type needs to be explicitly declared (True/False, Multiple Choice, Yes/No, Multi-Mode). Other types are recognized by Canvas from the QTI structure alone.
2. **Matching question transformation** — Cognero stores matching questions using `<response_grp>` elements; Canvas expects `<response_lid>` elements. The converter transforms the structure and normalizes answer identifiers across all match groups.
3. **File structure** — Output is a flat zip (`questions.xml`, `assessment_meta.xml`, `imsmanifest.xml` at the root) matching Canvas's expected item bank import layout.
4. **Assessment metadata** — A fresh `assessment_meta.xml` and `imsmanifest.xml` are generated with a new UUID-based assessment identifier.

## Limitations

- **Images and media**: Questions with embedded images or media files are not currently handled. The question text will convert correctly, but embedded image references will not resolve after import.
- **Multi-Blank blanks**: Cognero's Multi-Blank export does not include the individual blank definitions, so converted Multi-Blank questions will import as the fill_in_multiple_blanks_question type but without pre-configured blanks. You will need to set up the blanks manually in Canvas after import.

## Contributing

If you encounter a quiz file that doesn't convert correctly, please open an issue and attach the source zip file (or a sanitized version of it).

## AI Disclosure
This script was developed with assistance from Google Gemini and Claude Code. If you prefer not to use tools built with AI assistance, I completely understand.

## License
GPL3
