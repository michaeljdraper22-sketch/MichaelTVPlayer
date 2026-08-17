"""Helpers to derive country/region tokens from Xtream live category names.

Most IPTV providers organise Live TV categories by country with a separator,
e.g. "USA | Entertainment", "AR : Sports", "UK - News", "FR » Cinema".
We extract the leading token so users can filter by country/region.
"""

# Strong separators first (so mid-word hyphens aren't split), then " - ".
_SEPARATORS = ("|", ":", "»", "»", "/", "·", "•", " - ", " – ", " — ")


def extract_country(name: str) -> str:
    """Return the leading country/region token of a category name."""
    name = (name or "").strip()
    if not name:
        return ""
    for sep in _SEPARATORS:
        idx = name.find(sep)
        if idx > 0:
            return name[:idx].strip()
    return name


def group_categories_by_country(categories):
    """Return an ordered list of (country, [category, ...]) pairs."""
    groups = {}
    order = []
    for cat in categories or []:
        cname = cat.get("category_name") or str(cat.get("category_id", ""))
        country = extract_country(cname) or "Other"
        if country not in groups:
            groups[country] = []
            order.append(country)
        groups[country].append(cat)
    return [(c, groups[c]) for c in order]
