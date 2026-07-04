def valid_contract_id(value):
    if value is None:
        return False
    normalized = str(value).strip()
    return bool(normalized and normalized.casefold() not in {"none", "null", "undefined"})


def filter_valid_contracts(rows):
    """Broken legacy rows remain in SQLite but never enter URL-building UI contexts."""
    valid = []
    for row in rows or []:
        try:
            contract_id = row["contract_id"]
        except (KeyError, TypeError, IndexError):
            continue
        if valid_contract_id(contract_id):
            valid.append(row)
    return valid
