"""
slang_normalizer.py
Camellia Artisanal - Regional Slang & Colloquial Vocabulary Normalizer
Translates informal Hindi, Bengali, and Hinglish food terms into standard menu identifiers.
"""

import re
from typing import Dict, Any, List, Tuple

# Direct regex mapping tuples: (pattern, replacement)
SLANG_REPLACEMENTS: List[Tuple[str, str]] = [
    # Packaging / Box slang
    (r'\b(?:1\s*)?dabba\s+mishti\b', '1 box sondesh'),
    (r'\b(?:1\s*)?dabba\s+sandesh\b', '1 box sondesh'),
    (r'\bdabba\b|\bdibba\b|\bpeti\b|\bpacket\b', 'box'),
    (r'\bpackets\b|\bdabbas\b|\bpetis\b', 'boxes'),

    # Sweets & generic mishti terms
    (r'\bshondesh\b|\bsandesh\b|\bsondhesh\b', 'sondesh'),
    (r'(?<!\baamar\s)\b(?:mishti|misti|mithai|chhanar\s+mishti)\b|\bbengali\s+sweets?\b', 'sondesh'),

    # Jaggery & specific flavour slang
    (r'\bnotun\s+gur\b|\bkhajur\s+gur\b|\bdate\s+palm\s+jaggery\b|\bjhola\s+gur\b|\bpatali\s+gur\b', 'nolen gur'),
    (r'\bkachagolla\b|\bkancha\s+golla\b|\bkacha\s+gola\b', 'kanchagolla'),
    (r'\bjol\s+bhora\b|\bjolbhora\s+sandesh\b|\bliquid\s+center\s+sondesh\b', 'jolbhora'),
    (r'\baam\s+ada\b|\bmango\s+ginger\b', 'aam ada'),
    (r'\bdalchini\b', 'cinnamon'),
    (r'\bkeshar\b|\bzafran\b', 'kesar'),
    (r'\bgulab\b', 'rose'),

    # Kitchen & Bento slang
    (r'\bkosha\s+mangsho\s+bento\b|\bmangsho\s+bento\b', 'mutton bento'),
    (r'\bkosha\s+mangsho\b|\bmutton\s+kasha\b|\bsunday\s+mutton\b|\bmangsho\b', 'mutton curry'),
    (r'\bluchi\s+(?:aur|and|with)?\s*mangsho\b|\bluchi\s+mutton\b', 'mutton combo'),
    (r'\bluchi\s+(?:aur|and|with)?\s*al[ou]r?\s*dom\b', 'aloor dom combo'),
    (r'\bbasanti\s+pulao\b|\bmeetha\s+pulao\b|\byellow\s+rice\b', 'basonti polao'),
    (r'\bchingri\s+malai\b|\bprawn\s+malai\s+curry\b|\bprawn\s+curry\b', 'chingri malai curry'),
    (r'\bbhetki\s+fish\b|\bfish\s+paturi\b', 'bhetki paturi'),
    (r'\bveg\s+thali\b|\bbengali\s+veg\s+meal\b', 'veg bento'),
    (r'\bnon\s*veg\s+thali\b|\bbengali\s+non\s*veg\s+meal\b', 'non veg bento'),
]

def normalize_slang(text: str) -> str:
    """
    Normalizes colloquial Bengali/Hinglish phrasing to standard menu keywords.
    Example:
        'send 1 dabba mishti' -> 'send 1 box sondesh'
        'i want notun gur jolbhora and kosha mangsho bento' -> 'i want nolen gur jolbhora and mutton bento'
    """
    if not text:
        return ""
    
    cleaned = text
    for pattern, replacement in SLANG_REPLACEMENTS:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    
    return cleaned

def extract_slang_intent(text: str) -> Dict[str, Any]:
    """
    Extracts high-level signals and detected menu targets from regional slang.
    """
    text_lower = text.lower()
    signals = {
        "is_sweet_query": False,
        "is_meal_query": False,
        "normalized_text": normalize_slang(text),
        "detected_terms": []
    }

    if any(k in text_lower for k in ["mishti", "misti", "mithai", "sandesh", "shondesh", "dabba", "kachagolla", "jolbhora", "notun gur"]):
        signals["is_sweet_query"] = True
        signals["detected_terms"].append("artisanal_sweets")

    if any(k in text_lower for k in ["mangsho", "kosha", "bhaat", "bento", "luchi", "paturi", "chingri", "thali"]):
        signals["is_meal_query"] = True
        signals["detected_terms"].append("bengali_meals")

    return signals
