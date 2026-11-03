"""Academic text cleaning and boilerplate filtering utilities.

University LMS assignments, syllabi, rubrics, and module pages frequently contain
dense, legalistic boilerplates regarding academic integrity policies, Turnitin plagiarism
detection, and generative AI declarations.

These boilerplate sections cause two major issues for autonomous AI agents:
1. Token Burn: Wastes hundreds to thousands of context tokens on repetitive policy text.
2. Agent Hesitancy & False-Positive Refusals: Modern LLMs (GPT-4o, Claude, etc.) often
   interpret stern university disclaimers ("USE OF ARTIFICIAL INTELLIGENCE IS STRICTLY
   PROHIBITED") as instructions to refuse assisting the user with ordinary learning,
   concept explanations, or assignment structuring.

This module provides focused filters using standard-library regular expressions to excise
these boilerplate sections while strictly preserving legitimate assignment briefs, prompts,
learning outcomes, and substantive course concepts.
"""

from __future__ import annotations

import re

# Headings that mark entire sections of policy / declaration / misconduct boilerplate
_HEADING_BOILERPLATE_RE = re.compile(
    r"\b(?:"
    r"(?:student\s+)?academic\s+(?:integrity|honesty|misconduct)(?:\s+and\s+your\s+work)?"
    r"|chatgpt(?:\s+(?:warning|notice|information|policy|guidance|limitations))?"
    r"|use\s+of\s+(?:generative\s+)?(?:ai|artificial\s+intelligence|genai)"
    r"|(?:generative\s+)?(?:ai|artificial\s+intelligence|genai)\s+(?:policy|statement|rules|guidelines|declaration|usage|expectations|module|tools|in\s+assessment|prohibition|permitted|guidance|overview|support)"
    r"|declaration\s+of\s+(?:originality|academic\s+honesty|authorship)"
    r"|plagiarism(?:\s+and\s+collusion)?"
    r"|turnitin(?:\s+statement|\s+notice|\s+information)?"
    r"|citing,?\s+referencing\s+and\s+declaring\s+genai"
    r"|student\s+conduct\s+reports"
    r"|getting\s+help\s+from"
    r"|copying\s+the\s+answer"
    r"|protect\s+your\s+code"
    r"|working\s+and\s+discussing\s+with\s+friends"
    r"|thinking\s+of\s+using\s+ai"
    r"|lock\s+screen\s+when\s+leaving"
    r"|academic\s+honesty\s+training"
    r"|cornerstones\s+of\s+good\s+scholarship"
    r"|how\s+can\s+i\s+uphold\s+academic\s+integrity"
    r"|outsourcing\s+thats\s+not\s+cheating"
    r"|no\s+ai(?:\s+may\s+be\s+used)?"
    r"|project\s+development\s+required"
    r"|important\s+notice\s+for\s+developing"
    r"|what\s+is\s+(?:not\s+)?allowed"
    r"|all\s+code\s+for\s+this\s+project\s+must\s+be\s+developed"
    r")\b",
    re.IGNORECASE,
)

# Standalone institutional boilerplate paragraph regexes
_BOILERPLATE_PARAGRAPH_PATTERNS = [
    # University Student Academic Integrity Policy disclaimers
    re.compile(
        r"(?is)(?:(?:The\s+)?University(?:’s|'s)\s+)?(?:Student\s+)?Academic\s+Integrity\s+Policy\s*(?:\(MPF\d+\))?.*?(?:applies to all students|misconduct as defined|breach of the|educative response|principles of academic integrity).*?(?=\n\n|\Z)"
    ),
    # Use of AI software in assessment policies and declarations
    re.compile(
        r"(?is)(?:statement on the\s+)?use of (?:artificial intelligence|generative ai|genai|ai tools)(?: software)? in (?:the preparation of material for )?assessment.*?(?:familiarise yourself|strictly prohibited|not permitted|must be declared|academic integrity).*?(?=\n\n|\Z)"
    ),
    # Turnitin description notices
    re.compile(
        r"(?is)(?:Work submitted.*?via Turnitin|Turnitin is an online,?\s*web-based software that works by comparing).*?(?=\n\n|\Z)"
    ),
    # Legal admission / VLAB record-keeping warnings
    re.compile(
        r"(?is)A special note on academic integrity breaches,\s*record keeping and student conduct reports.*?(?=\n\n|\Z)"
    ),
    re.compile(
        r"(?is)The record-keeping section of the Student Academic Integrity Policy requires that the Academic Registrar.*?(?=\n\n|\Z)"
    ),
    # Statutory student misconduct definitions
    re.compile(
        r"(?is)A person commits student academic misconduct if he,?\s*or she,?\s*is a student and:.*?(?=\n\n|\Z)"
    ),
    # Declaration of originality / student pledge
    re.compile(
        r"(?is)(?:By submitting this (?:work|assessment|assignment),?\s*I|I hereby declare that this (?:work|submission|assignment)).*?(?:is my own work|all sources have been|plagiarism).*?(?=\n\n|\Z)"
    ),
    # Standalone AI declaration form prompts / citation examples
    re.compile(
        r"(?is)(?:AI Declaration|Generative AI Declaration|AI Usage Declaration):?\s*.*?(?:declared all tools|I have not used|prompts used).*?(?=\n\n|\Z)"
    ),
    re.compile(
        r"(?is)Complete the Academic Integrity Module\s*\(AIM\).*?(?=\n\n|\Z)"
    ),
    re.compile(
        r"(?is)\b(?:Self-plagiarism|Use of AI in assessment):.*?(?=\n\n|\n\d+\.|\Z)"
    ),
    # Template AI declaration headers and tables
    re.compile(
        r"(?is)(?:AI|Artificial Intelligence|Generative AI)\s+(?:Use|Usage|Assistance|Tools?)\s+Declaration.*?(?=\n\n|\Z)"
    ),
    # Template policy warnings regarding AI prohibition/restriction
    re.compile(
        r"(?is)(?:Carefully review (?:our )?policy on AI use.*?(?:must NOT use AI|strictly prohibited)|Note (?:that )?you must NOT use AI to generate (?:written work|visuals|content|text)).*?(?=\n\n|\Z)"
    ),
    # Declaration boilerplate acknowledgments and checkboxes
    re.compile(
        r"(?is)(?:We acknowledge the use of \[?AI tool|We did not use AI at any stage|Ai use is fully allowed for this assignment without declaration).*?(?=\n\n|\Z)"
    ),
    # Prompt / Use declaration form lines
    re.compile(
        r"(?is)Prompt:\s*We entered the following prompt.*?(?=\n\n|\Z)"
    ),
    # Warnings on plagiarism/collusion in guides
    re.compile(
        r"(?is)Students are warned to guard against any form of plagiarism or collusion.*?contact the subject coordinator.*?(?=\n\n|\Z)"
    ),
    # Collusion disclaimers and automated LMS detection warnings
    re.compile(
        r"(?is)\bCollusion is (?:strictly prohibited|regarded as academic misconduct).*?(?:punished accordingly|detect collusion).*?(?=\n\n|\Z)"
    ),
    re.compile(
        r"(?is)\bPlease also note that the LMS software contains inbuilt capabilities to detect collusion.*?(?=\n\n|\Z)"
    ),
    # Markdown TOC bullet links pointing to academic integrity sections
    re.compile(
        r"(?im)^\s*[-*]\s*\[(?:Student\s+)?Academic\s+Integrity[^\]]*\]\(#[^)]*\)\s*$"
    ),
    # Form declaration prompts in templates (e.g. sample OpenAI Desktop citation)
    re.compile(
        r"(?is)OpenAI\s*\(\d{4}\)\.\s*ChatGPT\s+on\s+your\s+Desktop\s*\(accessed\s+at\s+https?://openai\.com/.*?\)"
    ),
    # Project/assignment-specific AI prohibitions (e.g. "AI use is not permitted for this project")
    re.compile(
        r"(?im)^[ \t]*\**[ \t]*(?:Please\s+note\s+that\s+)?(?:AI\s+use|use\s+of\s+AI|Generative\s+AI|Artificial\s+Intelligence)\s+is\s+(?:not\s+permitted|strictly\s+prohibited|prohibited|banned)[^\n]*\**[ \t]*\n?"
    ),
    re.compile(
        r"(?is)(?:\*\*)?(?:Please\s+note\s+that\s+)?(?:AI\s+use|use\s+of\s+AI|Generative\s+AI|Artificial\s+Intelligence)\s+is\s+(?:not\s+permitted|strictly\s+prohibited|prohibited|banned).*?(?:within\s+Ed|for\s+this\s+project|in\s+this\s+subject|in\s+this\s+assessment).*?(?:\*\*)?(?=\n\n|\n|\Z)"
    ),
    re.compile(
        r"(?is)(?:\*\*)?(?:and\s+)?all\s+development\s+must\s+be\s+completed\s+within\s+Ed\.?(?:\*\*)?(?=\n\n|\n|\Z)"
    ),
    re.compile(
        r"(?is)\b(?:Thinking\s+of\s+using\s+AI[?]?\s*[-—]?\s*read\s+this\s+first).*?(?=\n\n|\Z)"
    ),
    re.compile(
        r"(?is)\b(?:Note\s+that\s+)?(?:students\s+must\s+not|you\s+must\s+not|do\s+not)\s+use\s+(?:AI|Artificial\s+Intelligence|ChatGPT|Copilot|Generative\s+AI)[^.\n]*\.*(?=\n\n|\n|\Z)"
    ),
    # Task reminder blockquotes and notices: "> Reminder: Any AI use is not allowed..."
    re.compile(
        r"(?im)^[ \t]*(?:>\s*)?(?:Reminder:\s*)?(?:Any\s+)?AI\s+use\s+is\s+not\s+(?:allowed|permitted)[^\n]*\n?"
    ),
    # EdStem platform requirement notices: "> You may use Python libraries which are available on the Ed platform."
    re.compile(
        r"(?im)^[ \t]*(?:>\s*)?You\s+may\s+use\s+Python\s+libraries\s+which\s+are\s+available\s+on\s+the\s+Ed\s+platform\.?[ \t]*\n?"
    ),
    # Standalone AI Use metadata lines: "**AI Use: **No AI may be used" or "**AI Allowed Use Statement: ...**"
    re.compile(
        r"(?im)^[ \t]*\**AI\s+(?:Allowed\s+)?Use(?:\s+Statement)?:\s*\**\s*(?:No\s+AI\s+may\s+be\s+used|Not\s+permitted)[^\n]*\n?"
    ),
    # GenAI prohibition paragraph: "This assessment does not allow any use of generative AI tools..."
    re.compile(
        r"(?is)This\s+assessment\s+does\s+not\s+allow\s+any\s+use\s+of\s+generative\s+AI\s+tools.*?(?=\n\n|\Z)"
    ),
    # Ed development mandatory notices with mark capping warnings
    re.compile(
        r"(?is)(?:>\s*)?(?:You\s+must\s+develop\s+and\s+complete\s+the\s+entire\s+project\s+inside\s+Ed|All\s+code\s+for\s+this\s+project\s+must\s+be\s+developed|If\s+any\s+of\s+your\s+submissions\s+for\s+any\s+of\s+the\s+tasks\s+does\s+\*+not\s*\*+show\s+clear\s+evidence\s+of\s+being\s+developed\s+inside\s+Ed).*?(?:capped\s+at\s+40%|affected).*?(?=\n\n|\Z)"
    ),
    # Hot tip paragraph about not using AI to avoid mark capping
    re.compile(
        r"(?is)(?:>\s*)?(?:\*\*)?Hot\s+tip:(?:\*\*)?\s*Developing\s+all\s+your\s+code\s+for\s+the\s+project.*?(?=\n\n|\Z)"
    ),
    # Link pointers to Project Development Required in Ed
    re.compile(
        r"(?im)^[ \t]*(?:>\s*)?Please\s+ensure\s+you\s+read\s+\[Project\s+Development\s+Required\s+in\s+Ed\][^\n]*\n?"
    ),
    # General integrity warning paragraphs in Ed lessons
    re.compile(
        r"(?is)Before\s+you\s+start\s+the\s+project,\s+you\s+must\s+watch\s+the\s+videos\s+and\s+complete\s+the\s+quiz\s+under\s+[“\"']CIS\s+Academic\s+Honesty\s+Training[”\"'].*?(?=\n\n|\Z)"
    ),
    re.compile(
        r"(?is)The\s+fastest\s+way\s+to\s+fail\s+the\s+subject\s+is\s+to\s+hand\s+in\s+code\s+that\s+is\s+not\s+your\s+own.*?(?=\n\n|\Z)"
    ),
    # Faculty / University-wide GenAI policies (e.g. FEIT / University of Melbourne)
    re.compile(
        r"(?im)^[ \t]*#{1,6}[ \t]*(?:\*\*)?[ \t]*\n?[ \t]*(?:FEIT\s+)?AI\s+Use\s+Policy(?:\*\*)?[ \t]*\n?"
    ),
    re.compile(
        r"(?is)(?:FEIT|[A-Z]{2,6}|Faculty|The\s+University)?\s*(?:allows|permits|regulates|policy\s+on)\s+(?:the\s+)?use\s+of\s+(?:Generative\s+AI|GenAI|Artificial\s+Intelligence).*?(?:academic\s+integrity\s+requirements|academic\s+integrity\s+breach|acknowledging\s+and\s+referencing|AI\s+Use\s+Statement|secure\s+assessment\s+tasks).*?(?=\n[ \t]*\n|\n[ \t]*#{1,6}\s|\Z)"
    ),
    re.compile(
        r"(?im)^[ \t]*If\s+anything\s+in\s+this\s+policy\s+is\s+unclear[^\n]*\n?"
    ),
    # Assignment guidance recommending students check the AI policy before starting
    re.compile(
        r"(?im)^[ \t]*(?:Before\s+starting\s+this\s+assignment,?\s*)?(?:you\s+should\s+)?(?:carefully\s+)?review\s+(?:the|our)\s+(?:AI|Generative\s+AI)\s+policy[^\n]*\n?"
    ),
    # ChatGPT / GenAI cautionary guidance and database comparisons in university study resources
    re.compile(
        r"(?is)ChatGPT\s+is\s+a\s+generative\s+artificial\s+intelligence\s*\(AI\).*?(?:trusted\s+library\s+databases|wastes\s+your\s+precious\s+study|cannot\s+'think'|look\s+very\s+convincing).*?(?=\n[ \t]*\n|\n[ \t]*#{1,6}\s|\Z)"
    ),
    # Interactive UI slider guidance for generative AI in developmental writing
    re.compile(
        r"(?im)^[ \t]*(?:#{1,6}\s*)?(?:[⭐*•-]\s*)?Use\s+the\s+slider\s+below\s+to\s+explore[^\n]*generative\s+AI[^\n]*\n?"
    ),
    # Standalone academic integrity and AI policy bullet links in course navigation
    re.compile(
        r"(?im)^\s*[-*]\s*\**Academic\s+integrity\**\s*\([^)]*\)\s*-\s*https?://[^\s]+\s*$"
    ),
    # Discussion forum surveillance, keystroke/run button tracking, and external pasting disclaimers
    re.compile(
        r"(?is)(?:I (?:also )?remember (?:teachers|staff|tutors) said )?(?:each time )?when a student clicks (?:the )?(?:run|check|test).*?(?:record will be generated|audit log).*?(?:copying|pasting|ai-generator|al-generator).*?(?=\n\n|\Z)"
    ),
    re.compile(
        r"(?is)(?:I just want to ask if I have created most codes|How can (?:teachers|staff|tutors) check whether someone's project is made by oneself).*?(?:satisfy to Integrity Requires|satisfying to Integrity Rules|Integrity Rules).*?(?=\n\n|\Z)"
    ),
    re.compile(
        r"(?im)^[ \t]*I'll have to defer the second question\.?[ \t]*\n?"
    ),
    re.compile(
        r"(?is)\n*##\s+Reply\s+\d+\s*\n+(?:>[^\n]*\n*)*(?:No,\s*this\s+is\s+fine\.\s*)?So\s+long\s+as\s+the\s+code\s+was\s+written\s+originally\s+in\s+Ed,\s+and\s+not\s+pasted\s+in\s+from\s+an\s+external\s+source,\s+there\s+is\s+no\s+issue\.?"
    ),
]


def filter_academic_boilerplate(text: str) -> str:
    """Filter out AI usage declarations, academic integrity rules, and Turnitin boilerplates.

    Preserves actual assignment instructions, prompts, rubrics, questions, and legitimate
    learning materials.

    Args:
        text: Plaintext or Markdown string to clean.

    Returns:
        Cleaned text with academic disclaimers and AI declaration rules stripped out.
    """
    if not text:
        return ""

    # Phase 1: Section-level removal based on Markdown headings
    lines = text.splitlines()
    filtered_lines: list[str] = []
    skipping_section = False
    current_skip_level = 0

    for line in lines:
        line_s = line.strip()
        m_head = re.match(r"^(#{1,4})\s+(.+)$", line_s)
        if m_head:
            hashes, raw_title = m_head.group(1), m_head.group(2).strip()
            # Strip markdown formatting (*, _, :) for reliable heading matching
            clean_title = re.sub(r"[*_:]+", "", raw_title).strip()
            level = len(hashes)
            if _HEADING_BOILERPLATE_RE.search(clean_title):
                skipping_section = True
                current_skip_level = level
                continue
            elif skipping_section and level <= current_skip_level:
                # Encountered a non-boilerplate section of equal or higher hierarchy
                skipping_section = False

        if skipping_section:
            continue
        filtered_lines.append(line)

    cleaned = "\n".join(filtered_lines)

    # Phase 2: Paragraph-level removal for standalone boilerplate disclaimers
    for pat in _BOILERPLATE_PARAGRAPH_PATTERNS:
        cleaned = pat.sub("", cleaned)

    # Phase 2b: Remove boilerplate title suffixes and empty orphan reply quotes
    cleaned = re.sub(r"(?i)\s+and\s+integrity\s+check\b", "", cleaned)
    cleaned = re.sub(r"(?im)^\s*##\s+Reply\s+\d+\s*\n+>\s*$", "", cleaned)

    # Phase 3: Normalize whitespace and redundant line breaks
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def is_academic_boilerplate_section(heading: str, body: str) -> bool:
    """Check whether a given heading or body chunk is predominantly academic boilerplate.

    Used by Knowledge Base parsers to avoid indexing disclaimers and AI declarations.
    """
    h_clean = heading.strip()
    if _HEADING_BOILERPLATE_RE.search(h_clean):
        return True

    h_lower = h_clean.lower()
    if any(phrase in h_lower for phrase in ("integrity check", "integrity rules", "integrity requires", "run and check records")):
        return True

    body_lower = body.lower()
    if any(phrase in body_lower for phrase in (
        "clicks the run",
        "check(blue) bottom",
        "al-generator through these records",
        "ai-generator through these records",
        "satisfy to integrity requires",
        "satisfying to integrity rules",
    )):
        return True

    # If the body is short and entirely matches a disclaimer pattern
    for pat in _BOILERPLATE_PARAGRAPH_PATTERNS:
        m = pat.search(body)
        if m and len(m.group(0)) >= len(body) * 0.7:
            return True

    return False


BOILERPLATE_PAGE_KEYWORDS = [
    "academic-integrity",
    "academic-honesty",
    "thinking-of-using-ai",
    "getting-help-from",
    "copying-the-answer",
    "protect-your-code",
    "working-and-discussing",
    "lock-screen-when-leaving",
    "outsourcing-thats-not-cheating",
    "student-conduct",
]


def is_boilerplate_page(name_or_title: str) -> bool:
    """Identify standalone academic integrity, plagiarism warning, and cheating stubs."""
    clean = name_or_title.lower().replace("_", "-").replace(" ", "-")
    return any(k in clean for k in BOILERPLATE_PAGE_KEYWORDS)


def is_academic_boilerplate_cue(cue_text: str) -> bool:
    """Check if an individual spoken subtitle cue is predominantly academic integrity or AI prohibition."""
    clean = re.sub(r"<[^>]+>", "", cue_text).strip().lower()
    return any(phrase in clean for phrase in (
        "we don't allow you to use ai",
        "we do not allow you to use ai",
        "don't allow you to use ai at all",
        "not allow you to use ai at all",
        "academic integrity and aiu",
        "academic integrity and ai",
        "same cap applies",
        "capped maximum 40%",
        "lacking evidence and your project mark will be capped",
    ))


def filter_vtt_content(vtt_raw: str) -> str:
    """Filter out spoken academic integrity, AI prohibition, and mark capping cues from WebVTT subtitle files.

    Preserves timing timestamps, WebVTT formatting, and substantive course lecture content.
    """
    if not vtt_raw:
        return ""

    cue_pattern = re.compile(
        r"((\d{2}:\d{2}(?::\d{2})?(?:\.\d{3})?)\s*-->\s*(\d{2}:\d{2}(?::\d{2})?(?:\.\d{3})?)\s*\n([\s\S]*?)(?=\n\n|\Z))"
    )

    cues = []
    for m in cue_pattern.finditer(vtt_raw):
        full_block = m.group(1)
        start_ts = m.group(2)
        end_ts = m.group(3)
        raw_text = m.group(4)
        clean_text = re.sub(r"<[^>]+>", "", raw_text).strip()
        cues.append({
            "full": full_block,
            "start": start_ts,
            "end": end_ts,
            "text": clean_text,
        })

    drop_indices: set[int] = set()
    n = len(cues)

    for i in range(n):
        # 1. Spoken AI prohibition: match sliding window
        window_5 = " ".join(cues[j]["text"] for j in range(i, min(i + 5, n))).lower()
        if any(kw in window_5 for kw in (
            "we don't allow you to use ai",
            "we do not allow you to use ai",
            "don't allow you to use ai at all",
            "not allow you to use ai at all",
            "do not allow you to use ai at all",
            "not allowed to use ai for this project",
            "must not use ai for this project",
        )):
            for j in range(i, min(i + 5, n)):
                t = cues[j]["text"].lower()
                if any(start_kw in t for start_kw in ["basically it's the same as project", "we don't allow", "the sense that"]):
                    drop_indices.add(j)
                elif "ai at all for this project" in t or "use ai at all" in t or "for this project" in t:
                    drop_indices.add(j)
                    break
                elif drop_indices and (j - 1) in drop_indices:
                    drop_indices.add(j)

        # 2. Ed Development Cap / Lacking evidence warnings
        t = cues[i]["text"].lower()
        if "same cap applies" in t or "evidence that your work was developed on end" in t or "evidence that your work was developed on ed" in t:
            for j in range(i, min(i + 12, n)):
                drop_indices.add(j)
                if "capped maximum 40%" in cues[j]["text"].lower() or "capped at 40%" in cues[j]["text"].lower():
                    break

        # 3. Spoken Academic Integrity & AI overview
        if "academic integrity and aiu" in t or "academic integrity and ai" in t:
            for j in range(i, min(i + 12, n)):
                drop_indices.add(j)
                if "ask the staff directly" in cues[j]["text"].lower():
                    if j + 1 < n and "and we can help you" in cues[j + 1]["text"].lower():
                        drop_indices.add(j + 1)
                    break

    if not drop_indices:
        return vtt_raw

    cleaned_blocks = [cues[i]["full"] for i in range(n) if i not in drop_indices]
    header_match = re.match(r"^([\s\S]*?)(?=\d{2}:\d{2}(?::\d{2})?(?:\.\d{3})?\s*-->)", vtt_raw)
    header = header_match.group(1) if header_match else "WEBVTT\n\n"
    return header.strip() + "\n\n" + "\n\n".join(cleaned_blocks) + "\n"

