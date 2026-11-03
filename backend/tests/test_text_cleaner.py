"""Tests for academic boilerplate and AI declaration filtering.

Verifies that academic integrity disclaimers, Turnitin notices, and generative AI declarations
are cleanly stripped from assignment descriptions, syllabus pages, and document readings,
while substantive subject concepts and assignment requirements are strictly preserved.
"""

from __future__ import annotations

import json
from unisync.core.text_cleaner import (
    filter_academic_boilerplate,
    is_academic_boilerplate_section,
)
from unisync.mcp.server import _clean_html_text, unisync_get_assignment_detail


def test_section_heading_boilerplate_removal() -> None:
    """Headings matching AI policy, academic integrity, or Turnitin should be stripped up to next section."""
    doc = """
# Assignment 1 - System Design

## Overview
Develop an interactive prototype for user onboarding.

## Academic Integrity & Plagiarism
The University's Student Academic Integrity Policy (MPF1310) applies to all students.
Any breaches will result in a zero mark and referral to the Academic Misconduct Committee.

## Use of Artificial Intelligence (AI)
Generative AI tools such as ChatGPT and Copilot are strictly forbidden in this assessment.
You must sign the AI declaration below.

## Grading Rubric
- Usability: 10 marks
- Architecture: 10 marks
"""
    cleaned = filter_academic_boilerplate(doc)
    assert "Develop an interactive prototype for user onboarding." in cleaned
    assert "Grading Rubric" in cleaned
    assert "Usability: 10 marks" in cleaned
    # Policy sections must be completely absent
    assert "MPF1310" not in cleaned
    assert "Academic Misconduct Committee" not in cleaned
    assert "ChatGPT and Copilot are strictly forbidden" not in cleaned


def test_standalone_disclaimer_paragraphs_removal() -> None:
    """Standalone paragraphs matching institutional policy templates should be removed."""
    text = (
        "Complete your draft by Friday 5 PM.\n\n"
        "Work submitted via the LMS is submitted via Turnitin, Turnitin is an online, "
        "web-based software that works by comparing electronically submitted papers to billions "
        "of pages of content located on the internet.\n\n"
        "Please submit in PDF format."
    )
    cleaned = filter_academic_boilerplate(text)
    assert "Complete your draft by Friday 5 PM." in cleaned
    assert "Please submit in PDF format." in cleaned
    assert "Turnitin is an online" not in cleaned


def test_preserves_legitimate_ai_course_content() -> None:
    """Legitimate course topics discussing artificial intelligence must NOT be stripped."""
    task_desc = """
## Project Brief: Health AI Assistant
Your task is to design an artificial intelligence system that assists nurses in patient triage.
Discuss how generative AI models can assist with diagnosis and evaluate ethical implications.
"""
    cleaned = filter_academic_boilerplate(task_desc)
    assert "Health AI Assistant" in cleaned
    assert "artificial intelligence system that assists nurses" in cleaned
    assert "generative AI models can assist with diagnosis" in cleaned


def test_html_cleaning_with_accordions() -> None:
    """Canvas accordion triggers and HTML headers with AI declarations are cleanly excised."""
    html_input = """
    <div class="uom-ui-accordion">
      <div class="uom-ui-accordion-trigger"><span class="uom-ui-accordion-title">Task Details</span></div>
      <div class="uom-ui-accordion-panel"><p>Create a 5-minute video pitch for your game.</p></div>
      <div class="uom-ui-accordion-trigger"><span class="uom-ui-accordion-title">Generative AI Policy</span></div>
      <div class="uom-ui-accordion-panel"><p>Students must not use generative AI for pitch voiceovers.</p></div>
      <div class="uom-ui-accordion-trigger"><span class="uom-ui-accordion-title">Submission</span></div>
      <div class="uom-ui-accordion-panel"><p>Upload your video URL to Canvas.</p></div>
    </div>
    """
    cleaned = _clean_html_text(html_input)
    assert "Create a 5-minute video pitch for your game." in cleaned
    assert "Upload your video URL to Canvas." in cleaned
    assert "Generative AI Policy" not in cleaned
    assert "Students must not use generative AI for pitch voiceovers." not in cleaned


def test_is_academic_boilerplate_section() -> None:
    """Validator accurately detects boilerplate headings and bodies."""
    assert is_academic_boilerplate_section("Academic Integrity", "Some policy text")
    assert is_academic_boilerplate_section("Generative AI Usage", "You must declare all tools")
    assert is_academic_boilerplate_section("Plagiarism and Collusion", "Don't copy work")
    assert not is_academic_boilerplate_section("Assignment Tasks", "Write a 500 word report")
    assert not is_academic_boilerplate_section("Machine Learning Architecture", "Train a neural network")


def test_template_docx_pdf_declaration_removal() -> None:
    """Verify template declaration tables, forms, and strict AI prohibition rules are stripped."""
    template_text = (
        "# Assignment 1.2: Design Proposals\n\n"
        "## Persona (1 page)\n"
        "Goals – What does your persona want and aim for?\n"
        "Frustrations – What obstacles are preventing your persona from getting what they want?\n\n"
        "Carefully review our policy on AI use in this subject in the corresponding module on the LMS. "
        "Note you must NOT use AI to generate written work or visuals in this assignment.\n\n"
        "AI Use Declaration\n\n"
        "We acknowledge the use of [AI tool or technology name] and [link] to generate... / We did not use AI at any stage of this assignment.\n"
        "Prompt: We entered the following prompt/s...\n"
        "Use: We used the output to.../We modified the output to...\n\n"
        "## Participant Table\n"
        "Participant Pseudonym | Gender | Age | Degree\n"
        "Yuqi | F | 19 | Bachelor of Design\n"
    )
    cleaned = filter_academic_boilerplate(template_text)
    assert "Goals – What does your persona want and aim for?" in cleaned
    assert "Participant Table" in cleaned
    assert "Yuqi | F | 19 | Bachelor of Design" in cleaned
    # All declaration and policy text must be stripped
    assert "AI Use Declaration" not in cleaned
    assert "We acknowledge the use of" not in cleaned
    assert "Note you must NOT use AI" not in cleaned


def test_project_ai_prohibition_removal() -> None:
    """Verify specific project announcements prohibiting AI use are cleanly removed."""
    announcement = (
        "## [2026-08-21] Project 1 Released on Ed\n\n"
        "Project 1 has now been released on Ed.\n\n"
        "**Please note that AI use is not permitted for this project, and all development must be completed within Ed.**\n\n"
        "You are welcome to start working on the project before Tuesday."
    )
    cleaned = filter_academic_boilerplate(announcement)
    assert "Project 1 has now been released on Ed." in cleaned
    assert "You are welcome to start working on the project before Tuesday." in cleaned
    assert "AI use is not permitted" not in cleaned
    assert "all development must be completed within Ed" not in cleaned


def test_edstem_project_lesson_cleaning() -> None:
    """Verify EdStem project lesson notices and task-level reminders are stripped while tasks remain."""
    from unisync.core.text_cleaner import filter_academic_boilerplate

    lesson_text = (
        "# Project 1\n\n"
        "- Module: Project Assignments\n"
        "**AI Use: **No AI may be used\n\n"
        "## **Important notice for developing and completing the project:**\n"
        "## **No AI may be used in this assessment**\n"
        "## This assessment does not allow any use of generative AI tools.\n"
        "## You must develop and complete the entire project inside Ed, since we will review your development history.\n\n"
        "## Project Development Required in Ed\n"
        "> Developing all your code for the project in its entirety inside Ed and without using AI...\n\n"
        "## What is allowed?\n"
        "- Code may be reused if developed by you in Ed.\n\n"
        "## What is not allowed?\n"
        "- Any AI use\n\n"
        "## Project Overview\n"
        "In this project, you will work with inventory records from Koala-Tee Outback Fashion.\n\n"
        "## Academic Integrity and AI Use\n"
        "**AI Allowed Use Statement: No AI may be used in this assessment**\n"
        "The fastest way to fail the subject is to hand in code that is not your own.\n\n"
        "## Task 1: Get Valid Inventory Records (3 marks)\n"
        "> Reminder: Any AI use is not allowed for this assessment and all project development is required in Ed.\n"
        "> You may use Python libraries which are available on the Ed platform.\n"
        "Write a function get_valid_records(inventory) which returns a list of valid records.\n"
    )
    cleaned = filter_academic_boilerplate(lesson_text)

    # Substantive content MUST be preserved
    assert "# Project 1" in cleaned
    assert "In this project, you will work with inventory records from Koala-Tee Outback Fashion." in cleaned
    assert "## Task 1: Get Valid Inventory Records (3 marks)" in cleaned
    assert "Write a function get_valid_records(inventory) which returns a list of valid records." in cleaned

    # Boilerplate sections and reminders MUST be stripped
    assert "No AI may be used" not in cleaned
    assert "Project Development Required in Ed" not in cleaned
    assert "Academic Integrity and AI Use" not in cleaned
    assert "Reminder: Any AI use is not allowed" not in cleaned
    assert "Python libraries which are available on the Ed platform" not in cleaned


def test_is_boilerplate_page() -> None:
    """Verify detection of academic honesty / cheating / misconduct stubs."""
    from unisync.core.text_cleaner import is_boilerplate_page

    assert is_boilerplate_page("comp10001_2026_sm2-pages-006-getting-help-from-non-student-friends.md")
    assert is_boilerplate_page("comp10001_2026_sm2-pages-007-copying-the-answer-from-a-fellow-student.md")
    assert is_boilerplate_page("comp10001_2026_sm2-pages-011-lock-screen-when-leaving-your-monitor.md")
    assert is_boilerplate_page("comp10001_2026_sm2-pages-012-protect-your-code-do-not-share-on-github.md")
    assert is_boilerplate_page("003-academic-integrity.md")
    assert is_boilerplate_page("013-thinking-of-using-ai-read-this-first.md")
    assert is_boilerplate_page("Page - Blaw10001_2026_Sm2 Pages 002 Student Conduct")
    assert is_boilerplate_page("Page - Blaw10001_2026_Sm2 Pages 003 Academic Integrity")

    # Regular course pages must NOT be flagged
    assert not is_boilerplate_page("00_Course_Overview_Syllabus.md")
    assert not is_boilerplate_page("Assignments_Guide.md")
    assert not is_boilerplate_page("EdStem Lesson 06 - Project 1")
    assert not is_boilerplate_page("Page - Staff Contact Details")


def test_feit_university_genai_policy_removal() -> None:
    """Verify Faculty/University GenAI policies in syllabi and briefs are cleanly stripped."""
    text = (
        "# COMP10001 Foundations of Computing\n\n"
        "## Course Overview\n"
        "Welcome to COMP10001! In this subject you will learn foundational Python programming.\n\n"
        "FEIT allows the use of Generative AI (GenAI) in open assessment tasks, unless the assessment "
        "instructions include a specific \"AI Use Statement\" that limits or prohibits the use. "
        "Using GenAI is not an academic integrity breach on its own. However, your submitted work "
        "must still be your own, and you remain fully responsible for meeting all academic integrity "
        "requirements - this includes properly acknowledging and referencing any GenAI you use. "
        "We encourage all students to take shared responsibility for their learning, and consider "
        "potential impacts on secure assessment tasks.\n\n"
        "## Weekly Schedule\n"
        "- Week 1: Introduction to Algorithms\n"
        "- Week 2: Control Flow\n"
    )
    cleaned = filter_academic_boilerplate(text)
    assert "Welcome to COMP10001!" in cleaned
    assert "## Weekly Schedule" in cleaned
    assert "Week 1: Introduction to Algorithms" in cleaned
    # FEIT policy must be completely removed
    assert "FEIT allows the use of Generative AI" not in cleaned
    assert "Using GenAI is not an academic integrity breach" not in cleaned
    assert "properly acknowledging and referencing" not in cleaned


def test_library_chatgpt_disclaimer_removal() -> None:
    """Verify ChatGPT caution paragraphs and academic integrity sections in study guides are stripped."""
    text = (
        "## Study Resources\n"
        "Access Southbank Library and Academic Skills workshops.\n\n"
        "### ChatGPT\n\n"
        "ChatGPT is a generative artificial intelligence (AI) built on a predictive language model and trained on open-access internet data.\n\n"
        "Remember that ChatGPT cannot 'think', and its answers to prompts might lead your research and writing in the wrong direction if not used with care.\n\n"
        "Librarians have already seen that ChatGPT tends to invent references to journal articles and other academic materials that don't actually exist, though the false information can look very convincing. Searching for non-existent literature, or verifying ChatGPT responses, wastes your precious study and writing time, so it's best to stick with trusted library databases.\n\n"
        "##### ⭐ Use the slider below to explore a developmental approach to writing in consideration of generative AI.\n\n"
        "## Writing Consultations\n"
        "Book a one-on-one consultation with an academic skills advisor."
    )
    cleaned = filter_academic_boilerplate(text)
    assert "Access Southbank Library and Academic Skills workshops." in cleaned
    assert "## Writing Consultations" in cleaned
    assert "Book a one-on-one consultation with an academic skills advisor." in cleaned
    # The entire ChatGPT boilerplate must be gone
    assert "### ChatGPT" not in cleaned
    assert "ChatGPT cannot 'think'" not in cleaned
    assert "trusted library databases" not in cleaned
    assert "Use the slider below" not in cleaned


def test_assignment_review_ai_policy_prompt_removal() -> None:
    """Verify inline prompts directing students to check the AI policy before starting an assignment are removed."""
    text = (
        "# Assignment 1 - User Needs Analysis\n\n"
        "In assignment 1, your task is to identify user needs and develop design proposals.\n\n"
        "Before starting this assignment, you should carefully review the AI policy for this subject: [AI Policy Link](https://canvas.lms.unimelb.edu.au)\n\n"
        "## Deliverables\n"
        "Submit a 10-page PDF report with interview transcripts."
    )
    cleaned = filter_academic_boilerplate(text)
    assert "In assignment 1, your task is to identify user needs" in cleaned
    assert "## Deliverables" in cleaned
    assert "Submit a 10-page PDF report with interview transcripts." in cleaned
    assert "review the AI policy for this subject" not in cleaned



