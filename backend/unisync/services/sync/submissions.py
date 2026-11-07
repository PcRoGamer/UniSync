"""Archive the authenticated student's Canvas submissions for local search."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from unisync.core.path_service import get_path_service


def safe_name(value: object) -> str:
    return re.sub(r'[^\w.()-]+', '_', str(value)).strip(' ._')[:100] or 'untitled'


def write_changed(path: Path, text: str) -> None:
    if not path.exists() or path.read_text(encoding='utf-8') != text:
        temporary = path.with_suffix(path.suffix + '.part')
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(path)


def clean_comments(raw_comments: object) -> list[dict]:
    """Keep readable Canvas feedback without persisting comment HTML or URLs."""
    if not isinstance(raw_comments, list):
        return []
    result = []
    for item in raw_comments:
        if isinstance(item, str):
            body = item
            comment = {}
        elif isinstance(item, dict):
            body = item.get('comment') or item.get('html_comment') or item.get('body') or ''
            comment = item
        else:
            continue
        text = BeautifulSoup(str(body), 'html.parser').get_text('\n', strip=True)
        record = {
            'author_name': comment.get('author_name'),
            'created_at': comment.get('created_at'),
            'updated_at': comment.get('updated_at'),
            'comment': text,
            'media_comment_id': comment.get('media_comment_id'),
            'media_comment_type': comment.get('media_comment_type'),
        }
        attachments = comment.get('attachments')
        if isinstance(attachments, list):
            record['attachments'] = [
                {key: attachment.get(key) for key in ('id', 'display_name', 'filename', 'content_type')
                 if attachment.get(key) is not None}
                for attachment in attachments if isinstance(attachment, dict)
            ]
        if (text or record.get('author_name') or record.get('created_at')
                or record.get('attachments') or record.get('media_comment_id')):
            result.append(record)
    return result


def clean_rubric_assessment(raw_assessment: object) -> dict:
    """Retain rubric points and readable criterion feedback without raw HTML."""
    if not isinstance(raw_assessment, dict):
        return {}
    cleaned = {}
    for criterion_id, result in raw_assessment.items():
        if not isinstance(result, dict):
            continue
        record = {
            key: result[key] for key in (
                'points', 'rating_id', 'description', 'criterion_description', 'rating_description'
            ) if key in result
        }
        comments = result.get('comments') or result.get('comments_html') or ''
        if comments:
            record['comments'] = BeautifulSoup(str(comments), 'html.parser').get_text('\n', strip=True)
        cleaned[str(criterion_id)] = record
    return cleaned


def has_meaningful_assessment(record: object) -> bool:
    """Capture submitted work AND grade-only assessment feedback.

    Canvas may give an assignment a score, rubric or marker comment without
    providing a submitted_at timestamp (on-paper, external-tool, quizzes, etc).
    Do not archive empty unsubmitted placeholder records.
    """
    if not isinstance(record, dict):
        return False
    if record.get('submitted_at'):
        return True
    if record.get('submission_type') not in (None, '', 'none'):
        return True
    if record.get('score') is not None or record.get('grade') is not None or record.get('graded_at'):
        return True
    if record.get('workflow_state') == 'graded':
        return True
    return any(bool(record.get(key)) for key in (
        'submission_comments', 'submission_html_comments',
        'rubric_assessment', 'full_rubric_assessment',
    ))


def rubric_feedback_lines(assignment: dict, assessment: object) -> list[str]:
    if not isinstance(assessment, dict) or not assessment:
        return []
    criteria = {
        str(item.get('id')): item.get('description') or item.get('long_description') or str(item.get('id'))
        for item in (assignment.get('rubric') or [])
        if isinstance(item, dict) and item.get('id') is not None
    }
    lines = []
    for criterion_id, result in assessment.items():
        if not isinstance(result, dict):
            continue
        title = criteria.get(str(criterion_id), str(criterion_id))
        points = result.get('points')
        rating = result.get('rating_id')
        comment = BeautifulSoup(str(result.get('comments') or result.get('comments_html') or ''),
                                'html.parser').get_text('\n', strip=True)
        details = []
        if points is not None:
            details.append(f'{points} points')
        if rating:
            details.append(f'rating {rating}')
        suffix = f" ({'; '.join(details)})" if details else ''
        lines.append(f'- {title}{suffix}')
        if comment:
            lines.append(f'  Feedback: {comment}')
    return lines


class SubmissionSync:
    def __init__(self, client: httpx.AsyncClient, root: Path):
        self.client = client
        self.root = root

    async def pages(self, url: str) -> list[dict]:
        result = []
        seen = set()
        while url and url not in seen:
            seen.add(url)
            response = await self.client.get(url)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                raise ValueError('Expected a Canvas list response')
            result.extend(data)
            url = response.links.get('next', {}).get('url', '')
            if url and urlparse(url).netloc != self.client.base_url.netloc.decode():
                raise ValueError('Unexpected Canvas pagination host')
        return result

    async def download(self, attachment: dict, path: Path) -> dict:
        if path.exists() and path.stat().st_size > 0:
            return {'status': 'cached', 'path': str(path)}
        temporary = path.with_suffix(path.suffix + '.part')
        try:
            url = attachment.get('url', '')
            if urlparse(url).scheme != 'https':
                raise ValueError('Attachment requires HTTPS')
            async with self.client.stream('GET', url, follow_redirects=True) as response:
                response.raise_for_status()
                if 'text/html' in response.headers.get('content-type', '').lower():
                    raise ValueError('Received a login page instead of an attachment')
                digest = hashlib.sha256()
                size = 0
                with temporary.open('wb') as output:
                    async for block in response.aiter_bytes():
                        size += len(block)
                        digest.update(block)
                        output.write(block)
                if not size:
                    raise ValueError('Empty attachment')
                expected = attachment.get('size')
                if expected is not None and size != int(expected):
                    raise ValueError('Incomplete attachment')
                temporary.replace(path)
            return {'status': 'downloaded', 'path': str(path), 'sha256': digest.hexdigest()}
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            # URLs may contain signed credentials; retain only the exception type.
            return {'status': 'failed', 'error': type(exc).__name__}

    async def archive(self, course: dict, assignment: dict, submission: dict) -> dict:
        course_name = safe_name(course.get('course_code') or course.get('name') or course['id'])
        assignment_id = safe_name(assignment['id'])
        attempt = safe_name(submission.get('attempt') or 1)
        prefix = f'canvas_{course["id"]}_{assignment_id}_attempt_{attempt}'
        folder = self.root / course_name / 'submissions' / assignment_id / f'attempt_{attempt}'
        folder.mkdir(parents=True, exist_ok=True)
        manifest = folder / 'submission.json'
        previous = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else {}
        files = []
        for attachment in submission.get('attachments') or []:
            filename = safe_name(attachment.get('filename') or attachment.get('display_name') or 'file')
            path = folder / f'{prefix}_{safe_name(attachment["id"])}_{filename}'
            record = await self.download(attachment, path)
            old = next((f for f in previous.get('files', []) if f.get('id') == attachment['id']), {})
            if record['status'] == 'cached' and old.get('sha256'):
                record['sha256'] = old['sha256']
            if record['status'] in ('cached', 'downloaded'):
                record['status'] = 'available'
            files.append({'id': attachment['id'], 'filename': filename, **record})
        feedback_only = not bool(submission.get('submitted_at') or submission.get('submission_type') not in (None, '', 'none'))
        metadata = {
            'record_kind': 'grade_or_feedback_only' if feedback_only else 'submitted',
            'course': course_name, 'course_id': course['id'],
            'assignment_id': assignment['id'], 'assignment': assignment.get('name', ''),
            'attempt': submission.get('attempt'), 'submitted_at': submission.get('submitted_at'),
            'submission_type': submission.get('submission_type'),
            'score': submission.get('score'), 'grade': submission.get('grade'),
            'graded_at': submission.get('graded_at'),
            'points_possible': assignment.get('points_possible'),
            'submission_comments': clean_comments(submission.get('submission_comments')),
            'submission_html_comments': clean_comments(submission.get('submission_html_comments')),
            'rubric_assessment': clean_rubric_assessment(submission.get('rubric_assessment')),
            'full_rubric_assessment': clean_rubric_assessment(submission.get('full_rubric_assessment')),
            'group_assignment': bool(assignment.get('group_category_id')),
            'body': submission.get('body'), 'url': submission.get('url'),
            'canvas_url': assignment.get('html_url'), 'files': files,
        }
        write_changed(manifest, json.dumps(metadata, indent=2, ensure_ascii=False))
        text = BeautifulSoup(submission.get('body') or '', 'html.parser').get_text('\n', strip=True)
        heading = 'Assessment feedback (no submitted-at timestamp)' if feedback_only else 'Submitted work'
        lines = [f'# {heading}: {metadata["assignment"]}', f'Course: {course_name}',
                 f'Canvas assignment: {metadata["canvas_url"]}', f'Attempt: {attempt}',
                 f'Submitted at: {metadata["submitted_at"]}',
                 f'Group assignment: {metadata["group_assignment"]}',
                 '\n## Submitted text\n', text, '\n## Submitted URL\n', submission.get('url') or '',
                 '\n## Attachments\n']
        lines.extend(f'- {f["filename"]}: {f["status"]}; {f.get("path", "")}' for f in files)
        lines.extend(['\n## Grade and feedback\n',
                      f'Score: {metadata["score"] if metadata["score"] is not None else "not graded"}'
                      + (f' / {metadata["points_possible"]}' if metadata["points_possible"] is not None else ''),
                      f'Grade: {metadata["grade"] if metadata["grade"] is not None else "not available"}',
                      f'Graded at: {metadata["graded_at"] or "not graded"}'])
        comments = metadata['submission_comments'] + metadata['submission_html_comments']
        if comments:
            lines.append('\n### Comments\n')
            for comment in comments:
                author = comment.get('author_name') or 'Canvas commenter'
                created = f" ({comment['created_at']})" if comment.get('created_at') else ''
                body = comment.get('comment') or (
                    f'[{comment["media_comment_type"]} media feedback]'
                    if comment.get('media_comment_type') else '[media feedback attached]'
                )
                lines.append(f'- {author}{created}: {body}')
        assessment = metadata['full_rubric_assessment'] or metadata['rubric_assessment']
        rubric_lines = rubric_feedback_lines(assignment, assessment)
        if rubric_lines:
            lines.extend(['\n### Rubric assessment\n', *rubric_lines])
        write_changed(folder / f'{prefix}_submission.md', '\n'.join(lines))
        return {'files': len(files), 'failed': sum(f['status'] == 'failed' for f in files),
                'feedback_only': int(feedback_only)}

    async def run(self, course_id: str | None = None) -> dict:
        counts = {'attempts': 0, 'feedback_only': 0, 'files': 0, 'failed_files': 0, 'errors': []}
        courses = await self.pages('/api/v1/courses?enrollment_type=student&enrollment_state=active&per_page=100')
        for course in courses:
            if course_id and str(course['id']) != str(course_id):
                continue
            try:
                assignments = await self.pages(
                    f'/api/v1/courses/{course["id"]}/assignments?per_page=100&include[]=submission&include[]=rubric'
                )
                for assignment in assignments:
                    summary = assignment.get('submission') or {}
                    if not has_meaningful_assessment(summary):
                        continue
                    try:
                        response = await self.client.get(
                            f'/api/v1/courses/{course["id"]}/assignments/{assignment["id"]}/submissions/self',
                            params=[
                                ('include[]', 'submission_history'),
                                ('include[]', 'submission_comments'),
                                ('include[]', 'submission_html_comments'),
                                ('include[]', 'rubric_assessment'),
                                ('include[]', 'full_rubric_assessment'),
                            ])
                        response.raise_for_status()
                        current = response.json()
                        attempts = {s.get('attempt') or 1: s for s in current.get('submission_history', [])
                                    if has_meaningful_assessment(s)}
                        if has_meaningful_assessment(current):
                            attempts[current.get('attempt') or 1] = current
                        for submission in attempts.values():
                            result = await self.archive(course, assignment, submission)
                            counts['attempts'] += 1
                            counts['files'] += result['files']
                            counts['failed_files'] += result['failed']
                            counts['feedback_only'] += result.get('feedback_only', 0)
                    except Exception as exc:
                        counts['errors'].append({'assignment_id': assignment['id'], 'error': type(exc).__name__})
            except Exception as exc:
                counts['errors'].append({'course_id': course['id'], 'error': type(exc).__name__})
        return counts


_lock = asyncio.Lock()
sync_status: dict = {'state': 'idle'}


async def sync_submissions(course_id: str | None = None) -> dict:
    """Share the resident browser's cookies for Canvas APIs and streamed files."""
    if _lock.locked():
        return {'state': 'already_running'}
    async with _lock:
        from unisync.services.browser.canvas_transport import CanvasSessionTransport
        sync_status.clear()
        sync_status.update(state='running')
        try:
            async with httpx.AsyncClient(base_url='https://canvas.lms.unimelb.edu.au',
                                         transport=CanvasSessionTransport(), timeout=60) as client:
                result = await SubmissionSync(client, get_path_service().get_user_root() / 'courses').run(course_id)
            sync_status.update(state='indexing', **result)
            from unisync.services.kb import get_kb_service
            indexed = await asyncio.to_thread(get_kb_service().index_all)
            while indexed.get('status') == 'busy-skipped':
                await asyncio.sleep(2)
                indexed = await asyncio.to_thread(get_kb_service().index_all)
            sync_status.update(state='done', index=indexed)
        except Exception as exc:
            sync_status.update(state='error', error=type(exc).__name__)
        return dict(sync_status)
