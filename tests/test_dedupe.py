import pytest

from jobpilot.db.dedupe import canonical_url, dedupe_hash


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.linkedin.com/jobs/view/4012345678/?refId=abc&trackingId=xyz%3D%3D&trk=public_jobs",
         "https://linkedin.com/jobs/view/4012345678"),
        ("https://in.linkedin.com/jobs/view/ai-business-analyst-at-acme-4012345678?position=3&pageNum=0",
         "https://linkedin.com/jobs/view/4012345678"),
        ("https://www.linkedin.com/jobs/search/?currentJobId=4012345678&keywords=ba",
         "https://linkedin.com/jobs/view/4012345678"),
        ("https://www.naukri.com/job-listings-business-analyst-acme-hyderabad-2-to-4-years-150926500123?src=jobsearchDesk&sid=1",
         "https://naukri.com/job-listings-150926500123"),
        ("HTTPS://Careers.Example.COM/jobs/42/?utm_source=x&b=2&a=1#apply",
         "https://careers.example.com/jobs/42?a=1&b=2"),
    ],
)
def test_canonical_url(url, expected):
    assert canonical_url(url) == expected


def test_dedupe_hash_ignores_noise():
    a = dedupe_hash("Acme Technologies Pvt. Ltd.", "AI Business Analyst", "Hyderabad, Telangana, India")
    b = dedupe_hash("acme technologies", "AI  Business-Analyst", "Hyderabad")
    assert a == b


def test_dedupe_hash_distinguishes_roles():
    assert dedupe_hash("Acme", "AI Business Analyst", "Hyderabad") != dedupe_hash("Acme", "Product Analyst", "Hyderabad")
