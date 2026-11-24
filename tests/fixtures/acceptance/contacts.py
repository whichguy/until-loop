import csv


def import_contacts(path):
    contacts = []
    seen_emails = set()
    with open(path, newline="", encoding="utf-8") as source:
        for row_number, row in enumerate(csv.DictReader(source), start=2):
            raw_email = (row.get("email") or "").strip()
            if not raw_email:
                raise ValueError(f"row {row_number} has no email")
            normalized_email = raw_email.lower()
            if normalized_email in seen_emails:
                continue
            seen_emails.add(normalized_email)
            contacts.append({
                "first_name": (row.get("first_name") or "").strip(),
                "last_name": (row.get("last_name") or "").strip(),
                "email": normalized_email,
                "phone": row.get("phone") or "",
                "company": row.get("company") or "",
            })
    return contacts
