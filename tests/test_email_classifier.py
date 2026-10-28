"""Tests for core.email_classifier — classification, status transitions, matching."""

from __future__ import annotations

import pytest

from core.email_classifier import (
    ACKNOWLEDGEMENT,
    ASSESSMENT,
    INTERVIEW,
    OFFER,
    OTHER,
    REJECTED,
    classify_email,
    match_application,
    next_status,
    normalize_company,
)


class TestClassify:
    @pytest.mark.parametrize(
        "subject,body,expected",
        [
            ("Update on your Acme application",
             "Thank you for your interest. Unfortunately, we have decided to move forward with other candidates.",
             REJECTED),
            ("Your application", "We regret to inform you that we will not be moving forward with your application.", REJECTED),
            ("Re: application", "We are not able to extend you an offer of employment.", REJECTED),
            ("Your application", "Unfortunately we are unable to offer you the role at this time.", REJECTED),
            ("Interview with Acme", "We'd like to schedule an interview with you. Book via calendly.com/acme/30", INTERVIEW),
            ("Next steps", "I would like to speak with you about the role. What is your availability this week?", INTERVIEW),
            ("Offer from Acme", "We are pleased to offer you the position. Please see the offer letter.", OFFER),
            ("Thanks for applying", "We have received your application and will review it.", ACKNOWLEDGEMENT),
            ("Next step", "Please complete the online assessment on HackerRank within 7 days.", ASSESSMENT),
            ("Weekly digest", "Top jobs for you. 10 interview tips. Salary guide.", OTHER),
            ("Hello", "", OTHER),
        ],
    )
    def test_labels(self, subject, body, expected):
        assert classify_email(subject, body).label == expected

    def test_rejection_beats_soft_forward_language(self):
        c = classify_email("Moving forward", "We are not moving forward with your application, though we'd love to chat again someday.")
        assert c.label == REJECTED

    def test_weak_signal_alone_is_other(self):
        # "Unfortunately, we have to reschedule" must not look like a rejection
        c = classify_email("Interview reschedule", "Unfortunately, we have to reschedule your interview. Please pick another time.")
        assert c.label == OTHER

    def test_negated_offer_phrase_not_an_offer(self):
        c = classify_email("Update", "We are not able to extend you an offer at this time.")
        assert c.label != OFFER

    def test_curly_apostrophes_are_normalised(self):
        c = classify_email("Update", "We won\u2019t be moving forward with your candidacy.")
        assert c.label == REJECTED

    def test_confidence_in_range(self):
        c = classify_email("Offer", "We are pleased to offer you the job. Offer letter attached. Formal offer enclosed.")
        assert 0.9 <= c.confidence <= 0.99


class TestNextStatus:
    @pytest.mark.parametrize(
        "current,label,expected",
        [
            ("applied", REJECTED, "rejected"),
            ("manual_required", REJECTED, "rejected"),
            ("interview", REJECTED, "rejected"),
            ("applied", INTERVIEW, "interview"),
            ("reviewed", INTERVIEW, "interview"),
            ("applied", OFFER, "offer"),
            ("interview", OFFER, "offer"),
            # never downgrade / override
            ("offer", REJECTED, None),
            ("interview", INTERVIEW, None),
            ("offer", INTERVIEW, None),
            ("accepted", REJECTED, None),
            ("withdrawn", REJECTED, None),
            ("rejected", INTERVIEW, None),
            # never-submitted applications are ignored
            ("error", REJECTED, None),
            ("skipped", OFFER, None),
            ("saved", INTERVIEW, None),
            # non-actionable labels never change status
            ("applied", ACKNOWLEDGEMENT, None),
            ("applied", ASSESSMENT, None),
        ],
    )
    def test_transitions(self, current, label, expected):
        assert next_status(current, label) == expected


APPS = [
    {"id": 1, "company": "Acme Inc.", "job_title": "Senior Software Engineer"},
    {"id": 2, "company": "Acme Inc.", "job_title": "Data Analyst"},
    {"id": 3, "company": "Globex Corporation", "job_title": "Backend Engineer"},
]


class TestMatching:
    def test_normalize_company_strips_suffixes(self):
        assert normalize_company("Acme, Inc.") == "acme"
        assert normalize_company("Globex Corporation") == "globex"
        assert normalize_company("Inc") == "inc"  # a lone token is kept

    def test_title_disambiguates_same_company(self):
        m = match_application(APPS, "Acme Recruiting", "no-reply@greenhouse.io",
                              "Your Senior Software Engineer application at Acme", "")
        assert m.application_id == 1 and not m.ambiguous and m.score >= 2

    def test_same_company_no_title_is_ambiguous(self):
        m = match_application(APPS, "Acme Recruiting", "no-reply@greenhouse.io", "Update from Acme", "")
        assert m.ambiguous

    def test_sender_domain_match(self):
        m = match_application(APPS, "Jane", "jane@globex.com", "Hello", "hi")
        assert m.application_id == 3 and "sender domain" in m.reasons

    def test_generic_ats_domain_is_not_evidence(self):
        m = match_application(APPS, "Recruiting", "x@greenhouse.io", "Hello", "no company here")
        assert m is None

    def test_title_alone_never_matches(self):
        m = match_application(APPS, "Someone", "a@b.com", "Backend Engineer opening", "Backend Engineer role")
        assert m is None

    def test_body_only_mention_scores_low(self):
        m = match_application(APPS, "Agency", "x@agency.com", "Role", "our client Globex Corporation is hiring")
        assert m.application_id == 3 and m.score == 1

    def test_whole_word_only(self):
        apps = [{"id": 9, "company": "Ace", "job_title": "Engineer"}]
        assert match_application(apps, "Space Corp", "a@space.io", "Palace job", "terrace palace") is None
