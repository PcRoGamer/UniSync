"""EdStem feedback and grade fidelity tests, no network or personal data."""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
import json
from urllib.parse import urlparse

import pytest

from unisync.services.edstem.client import (
    EdStemClient, EdStemError, EdStemSnapshot, extract_due_items
)
from unisync.services.edstem.submissions import archive_edstem_assessments

class Reply:
    def __init__(self, data, status=200):
        self.data=data
        self.status_code=status
        self.ok=status==200
        self.headers={}
    def json(self):return self.data

class Session:
    def __init__(self, *, unreleased=False, override=None):
        self.unreleased=unreleased
        self.override=override
        self.calls=[]
    def get(self,url,**kw):
        assert kw["headers"]["Authorization"] == "Bearer fake-token"
        path=urlparse(url).path
        self.calls.append((path,kw.get("params")))
        if path.endswith("/user"):return Reply({"user":{"id":77}})
        if path.endswith("/lessons/5/attempts/77"):
            return Reply({"final_id":11,"attempts":[
                {"id":11,"user_id":77,"submitted_at":"2026-10-01T04:00:00Z",
                 "mark_override":self.override,"comment":"Nice work"},
                {"id":12,"user_id":999,"submitted_at":"2026-10-02T00:00:00Z"},
            ]})
        if path.endswith("/lessons/5/marking_status"):
            if self.unreleased:return Reply({"code":"results_unreleased"},403)
            return Reply({
                "attempt_result":{"user_id":77,"mark":6,
                                  "final_comment":"Review edge cases.",
                                  "slides":[
                                      {"slide_id":100,"title":"Analysis","user_id":77,"marks":4,"rubric_mark":3,"auto_mark":1,
                                       "feedback":"Needs more tests"},
                                      {"slide_id":101,"user_id":999,"title":"Other student","marks":20,"feedback":"PRIVATE"},
                                  ]},
                "slide_totals":{"100":8,"102":2},
                "lesson_markable_marking_status":[{"unmarked_user_ids":[999]}],
            })
        if path.endswith("/lessons/slides/100/questions/responses"):
            return Reply({"responses":[
                {"question_id":1,"user_id":77,"correct":False,"data":{"choice":[1]}},
                {"question_id":2,"user_id":999,"correct":True,"data":{"choice":[2]}},
            ]})
        raise AssertionError("Unexpected GET: "+path)

def lessons():
    return [
        {"id":5,"kind":"attempt","status":"completed","submitted_at":"2026-10-01T04:00:00Z",
         "title":"Assignment 1","due_at":"2026-10-01T00:00:00Z","course_id":99,
         "slides":[{"id":100,"type":"quiz"}]},
        {"id":6,"kind":"attempt","status":"attempted","submitted_at":None,
         "title":"Assignment 2","due_at":"2026-11-01T00:00:00Z","course_id":99},
        {"id":7,"kind":"content","status":"completed","title":"Reading (2 points)",
         "due_at":"2026-09-01T00:00:00Z","course_id":99},
    ]

def test_true_mark_is_archived_and_only_authenticated_user(tmp_path):
    session=Session()
    client=EdStemClient("fake-token",session=session)
    result=archive_edstem_assessments(client,lessons(),tmp_path)
    summary=result["summary"]
    assert summary["attempt_lessons"]==2
    assert summary["submitted_lessons"]==1
    assert summary["graded_lessons"]==1
    assert summary["graded_slides"]==1
    grade=result["grades_by_lesson"]["5"]
    assert grade["score"]==6
    assert grade["points_possible"]==10
    assert grade["status"]=="graded"
    assert result["grades_by_lesson"]["6"]["status"]=="not_submitted"
    archive=json.loads((tmp_path/"edstem"/"submissions"/"5"/"submission.json").read_text("utf-8"))
    assert len(archive["attempts"])==1
    assert archive["grade"]["final_comment"]=="Review edge cases."
    assert archive["quiz_responses"]["100"][0]["correct"] is False
    assert "PRIVATE" not in json.dumps(archive)
    assert not any(p.endswith("/6/attempts/77") for p,_ in session.calls)

def test_override_zero_takes_priority_over_auto_mark(tmp_path):
    result=archive_edstem_assessments(EdStemClient("fake-token",session=Session(override=0)),
                                    lessons(),tmp_path)
    assert result["grades_by_lesson"]["5"]["score"]==0
    assert result["grades_by_lesson"]["5"]["grade_source"]=="edstem_final_attempt_override"

def test_unreleased_is_not_auth_failure_and_no_grade(tmp_path):
    result=archive_edstem_assessments(EdStemClient("fake-token",session=Session(unreleased=True)),
                                    lessons(),tmp_path)
    assert result["summary"]["unreleased_lessons"]==1
    assert result["grades_by_lesson"]["5"]["score"] is None
    assert result["grades_by_lesson"]["5"]["status"]=="unreleased"

def test_completed_content_never_fabricates_submitted_or_graded():
    snapshot=EdStemSnapshot(course={"id":99},modules=[],lessons=lessons(),resources=[],
                            threads=[],announcements=[],fetched_at="")
    items=extract_due_items(snapshot,verified_grades={"5":{"status":"graded","score":6,"grade":"6","points_possible":10}})
    attempt,other,content=items
    assert attempt["submission_status"]=="graded"
    assert attempt["score"]==6
    assert attempt["is_graded"] is True
    assert other["submission_status"]=="in_progress"
    assert other["score"] is None
    assert content["submission_status"]=="not_applicable"
    assert content["status"]=="done"
    assert content["is_graded"] is False
    assert content["points_possible"]==2
    assert content["score"] is None

def test_no_attempt_lessons_makes_no_auth_request(tmp_path):
    class NoClient:
        def _request(self,*args,**kwargs):raise AssertionError("No request expected")
    result=archive_edstem_assessments(NoClient(),[{"id":8,"kind":"content"}],tmp_path)
    assert result["grades_by_lesson"]=={}
    assert result["summary"]["submitted_lessons"]==0

def test_marking_denies_wrong_user_even_with_valid_token(tmp_path):
    class Wrong(Session):
        def get(self,url,**kwargs):
            response=super().get(url,**kwargs)
            if urlparse(url).path.endswith("/marking_status") and response.ok:
                response.data["attempt_result"]["user_id"]=999
            return response
    with pytest.raises(EdStemError,match="another user|authenticated user"):
        client=EdStemClient("fake-token",session=Wrong())
        client.fetch_own_marking_status("5","11","77")
