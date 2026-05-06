import json
import os
import requests


def unwrap(res):
    data = res.json()
    if isinstance(data, dict):
        inner = data.get("data")
        # Only unwrap when "data" key is present AND its value is a non-None dict/list.
        # If "data" is null the API signalled an error; fall back to the outer envelope
        # so callers can inspect "error" / "message" fields instead of receiving None.
        if inner is not None:
            return inner
        return data
    return data


def search_with_api_key(session, query, *, api_key, base_url, port):
    payload = {
        "api_key": api_key,
        "q": query,
        "fields": ["ttl", "ab", "clm", "desc"],
        "rows": 1,
        "filters": [],
        "cursorMark": "*",
        "sort": [],
        "extraParams": {},
    }

    if "proxy" in base_url:
        res = session.post(
            f"{base_url}/search",
            data={"port": port, "input": json.dumps(payload)},
            timeout=30,
        )
    else:
        res = session.post(
            f"{base_url}/search",
            json=payload,
            headers={"X-API-Key": api_key},
            timeout=30,
        )

    res.raise_for_status()
    return unwrap(res)


def parse_claims_with_api_key(session, xml, *, api_key, base_url, port):
    payload: dict = {
        "api_key": api_key,
        "xml": xml,
        "parse_xml": True,
    }

    if "proxy" in base_url:
        res = session.post(
            f"{base_url}/parse_claims",
            data={"port": port, "input": json.dumps(payload)},
            timeout=30,
        )
    else:
        res = session.post(
            f"{base_url}/parse_claims",
            json=payload,
            headers={"X-API-Key": api_key},
            timeout=30,
        )

    res.raise_for_status()
    return unwrap(res)


def parse_description_with_api_key(session, xml, *, api_key, base_url, port):
    payload = {
        "api_key": api_key,
        "xml": xml,
        "parse_xml": True,
    }

    if "proxy" in base_url:
        res = session.post(
            f"{base_url}/parse_description",
            data={"port": port, "input": json.dumps(payload)},
            timeout=30,
        )
    else:
        res = session.post(
            f"{base_url}/parse_description",
            json=payload,
            headers={"X-API-Key": api_key},
            timeout=30,
        )

    res.raise_for_status()
    return unwrap(res)


def _blocks_to_text(blocks: list) -> str:
    """Join claim blocks into a single readable string."""
    parts = []
    for block in blocks:
        text = block.get("text", "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def _norm_num(val) -> int:
    """Parse a claim number that may be zero-padded ('00001' → 1)."""
    try:
        return int(str(val))
    except (ValueError, TypeError):
        return -1


def _extract_claim(parsed: dict, claim_number: int) -> dict:
    """Pull the requested claim dict out of a parse_claims response."""
    # Some API versions return a 'claims' list
    claims_list = parsed.get("claims", [])
    if claims_list:
        for c in claims_list:
            if _norm_num(c.get("num")) == claim_number:
                return c
        available = [_norm_num(c.get("num")) for c in claims_list]
        raise ValueError(
            f"Claim {claim_number} not found in patent. "
            f"Available claim numbers: {available}"
        )

    # Fallback: single 'claim' key (API returned only the requested claim)
    claim = parsed.get("claim", {})
    if not claim:
        raise ValueError("No claims found in the parsed response.")

    returned_num = _norm_num(claim.get("num"))
    total = parsed.get("total_claims", "?")
    if returned_num != -1 and returned_num != claim_number:
        raise ValueError(
            f"Requested claim {claim_number} but the API returned claim "
            f"{returned_num} (total claims: {total}). "
            "The PCS API does not support selecting by claim number directly. "
            "Only claim 1 (the independent claim) can be fetched automatically."
        )

    return claim


def fetch_claim_from_patent(
    patent_number: str,
    claim_number: int = 1,
    *,
    api_key: str,
    base_url: str,
    port: str,
) -> str:
    """Return the text of *claim_number* from *patent_number* via the PCS API.

    Args:
        patent_number: Patent identifier, e.g. ``"US-20120212660-A1"``.
        claim_number:  1-indexed claim to fetch. Defaults to 1 (first claim).
        api_key:       PCS API key.
        base_url:      PCS API base URL.
        port:          PCS API port string.

    Returns:
        Claim text as a plain string (blocks joined with newlines).

    Raises:
        ValueError: Patent not found, no claim XML, or claim number absent.
        requests.HTTPError: HTTP-level failure talking to the PCS API.
    """
    session = requests.Session()
    creds = {"api_key": api_key, "base_url": base_url, "port": port}

    pn = patent_number.strip()
    result = search_with_api_key(session, f'pn:"{pn}"', **creds)
    docs = result.get("docs", [])
    if not docs:
        raise ValueError(f"Patent '{pn}' not found via PCS API.")

    clm = docs[0].get("clm")
    clm_xml = clm[0] if isinstance(clm, list) else clm
    if not clm_xml:
        raise ValueError(f"No claim XML found for patent '{pn}'.")

    parsed = parse_claims_with_api_key(session, clm_xml, **creds)
    claim = _extract_claim(parsed, claim_number)
    text = _blocks_to_text(claim.get("blocks", []))
    if not text.strip():
        raise ValueError(
            f"Claim {claim_number} of '{pn}' parsed but produced empty text."
        )
    return text


def fetch_patent_claim_and_description(
    patent_number: str,
    claim_number: int = 1,
    *,
    api_key: str,
    base_url: str,
    port: str,
) -> tuple[str, list[str]]:
    """Return ``(claim_text, description_paragraphs)`` in one API round-trip.

    Args:
        patent_number:  Patent identifier, e.g. ``"US-20120212660-A1"``.
        claim_number:   1-indexed claim to fetch. Defaults to 1.
        api_key:        PCS API key.
        base_url:       PCS API base URL.
        port:           PCS API port string.

    Returns:
        Tuple of (claim_text, paragraphs) where paragraphs is a list of
        plain-string description paragraphs (empty list when not available).

    Raises:
        ValueError: Patent not found, no claim XML, or claim number absent.
        requests.HTTPError: HTTP-level failure talking to the PCS API.
    """
    session = requests.Session()
    creds = {"api_key": api_key, "base_url": base_url, "port": port}

    pn = patent_number.strip()
    result = search_with_api_key(session, f'pn:"{pn}"', **creds)
    docs = result.get("docs", [])
    if not docs:
        raise ValueError(f"Patent '{pn}' not found via PCS API.")
    doc = docs[0]

    # --- Claim ---
    clm = doc.get("clm")
    clm_xml = clm[0] if isinstance(clm, list) else clm
    if not clm_xml:
        raise ValueError(f"No claim XML found for patent '{pn}'.")
    parsed_claims = parse_claims_with_api_key(session, clm_xml, **creds)
    claim_dict = _extract_claim(parsed_claims, claim_number)
    claim_text = _blocks_to_text(claim_dict.get("blocks", []))
    if not claim_text.strip():
        raise ValueError(
            f"Claim {claim_number} of '{pn}' parsed but produced empty text."
        )

    # --- Description (graceful degradation: empty list when absent) ---
    desc = doc.get("desc")
    desc_xml = desc[0] if isinstance(desc, list) else desc
    paragraphs: list[str] = []
    if desc_xml:
        parsed_desc = parse_description_with_api_key(session, desc_xml, **creds)
        paragraphs = [
            str(p).strip()
            for p in parsed_desc.get("paragraphs", [])
            if str(p).strip()
        ]

    return claim_text, paragraphs


def _pcs_creds_from_env() -> dict:
    return {
        "api_key": os.environ.get("PCS_API_KEY", ""),
        "base_url": os.environ.get("PCS_API_BASE_URL", ""),
        "port": os.environ.get("PCS_API_PORT", ""),
    }


def main():
    from dotenv import load_dotenv
    load_dotenv()

    creds = _pcs_creds_from_env()
    session = requests.Session()

    # --- Step 1: Search ---
    result = search_with_api_key(session, 'pn:"US7629884B2"', **creds)
    docs = result.get("docs", [])

    if not docs:
        print("No documents found.")
        return

    # --- Step 2: Extract claim XML ---
    clm = docs[0].get("clm")
    clm_xml = clm[0] if isinstance(clm, list) else clm

    if not clm_xml:
        print("No claim XML found.")
        return

    # --- Step 3: Parse claims ---
    parsed = parse_claims_with_api_key(session, clm_xml, **creds)

    print(f"Total claims: {parsed.get('total_claims')}\n")

    claim = parsed.get("claim", {})
    print(f"Claim {claim.get('num')}:")

    for block in claim.get("blocks", []):
        indent = " " * (4 + block["indent"])
        print(f"{indent}{block['text']}")

    # --- Step 4: Extract description XML ---
    desc = docs[0].get("desc")
    desc_xml = desc[0] if isinstance(desc, list) else desc

    if not desc_xml:
        print("\nNo description XML found.")
        return

    # --- Step 5: Parse description ---
    parsed_desc = parse_description_with_api_key(session, desc_xml, **creds)

    print(f"\nTotal description paragraphs: {parsed_desc.get('total_paragraphs')}\n")

    for para in parsed_desc.get("paragraphs", []):
        print(f"{para}")


if __name__ == "__main__":
    main()
