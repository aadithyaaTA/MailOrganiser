import sys
import datetime
import py_compile

# Compile check for app.py
try:
    py_compile.compile("app.py", doraise=True)
    print("SUCCESS: app.py compiled without syntax errors.")
except Exception as e:
    print(f"ERROR: app.py failed compilation: {e}")
    sys.exit(1)

# Import module functions from app.py
from app import (
    process_email_deadline,
    calculate_urgency,
    parse_email_date,
    TRIGGER_PHRASES,
    COMBINED_DATE_REGEX
)

print("\n--- Testing Trigger Phrases & Date Extractions ---")
test_cases = [
    {
        "body": "Hi John, please respond by 15 August so we can confirm your slot. Thanks!",
        "expected_deadline": "15/08/2026", # or current year
        "expected_context": "Hi John, please respond by 15 August so we can confirm your slot."
    },
    {
        "body": "The report is due by 25/12/2026. Please complete it soon.",
        "expected_deadline": "25/12/2026",
        "expected_context": "The report is due by 25/12/2026."
    },
    {
        "body": "RSVP by Dec 31, 2026 to save your seat.",
        "expected_deadline": "31/12/2026",
        "expected_context": "RSVP by Dec 31, 2026 to save your seat."
    },
    {
        "body": "Please confirm by the 10th of September for attendance.",
        "expected_deadline": "10/09/2026",
        "expected_context": "Please confirm by the 10th of September for attendance."
    },
    {
        "body": "Just checking in about yesterday's meeting.",
        "expected_deadline": None,
        "expected_context": None
    }
]

for idx, tc in enumerate(test_cases, 1):
    dt, dl_str, ctx = process_email_deadline(tc["body"])
    print(f"Test {idx}:")
    print(f"  Body: {tc['body']}")
    print(f"  Result Deadline: {dl_str}")
    print(f"  Result Context:  {ctx}")

print("\n--- Testing Urgency Calculation ---")
today = datetime.datetime.now()
past_dt = today - datetime.timedelta(days=2)
urgent_dt = today + datetime.timedelta(days=1)
soon_dt = today + datetime.timedelta(days=5)
upcoming_dt = today + datetime.timedelta(days=12)

print(f"Past (-2d) -> {calculate_urgency(past_dt)} (Expected: overdue)")
print(f"Urgent (+1d) -> {calculate_urgency(urgent_dt)} (Expected: urgent)")
print(f"Soon (+5d) -> {calculate_urgency(soon_dt)} (Expected: soon)")
print(f"Upcoming (+12d) -> {calculate_urgency(upcoming_dt)} (Expected: upcoming)")

print("\n--- Testing Email Date Parsing ---")
_, iso = parse_email_date("Mon, 01 Aug 2025 10:23:00 +0000")
print(f"Parsed Date Header: {iso} (Expected: 2025-08-01T10:23:00Z)")

print("\nAll unit tests finished.")
