"""Reading a customer's meaning without a language model.

This is what runs when the understanding provider is unreachable. It is not a
trigger list: it holds *concepts* — the words and short phrase shapes that mean
"price" or "how do I order" in the English and Hinglish this kitchen's customers
actually write — and it maps them to the same validated intents and entities the
model returns. Adding a concept teaches the whole family; adding a sentence
teaches one customer.

Three properties matter more than coverage:

* Clauses are read separately. "Show me the menu and can I bring my dog?" is two
  questions, and the second one must not disappear because the first is
  answerable.
* A clause that carries meaning but matches no concept becomes
  `unknown_business_query` — a typed gap, not silence. That works for topics
  nobody listed, which is the point.
* Confidence stays conservative: this reader never claims the certainty of a
  model, and everything it produces is still checked against approved knowledge
  before a customer sees anything.
"""

import re
from typing import Any, Dict, List, Optional, Set, Tuple

# --- normalisation ---------------------------------------------------------

# Hinglish spelling drifts. Only the forms that change meaning are folded.
_SPELLING = {
    "sandesh": "sondesh", "sondes": "sondesh", "mithai": "mishti",
    "gurugram": "gurgaon", "veg.": "veg", "nonveg": "non veg",
    "non-veg": "non veg", "nonvegetarian": "non vegetarian",
    "kitne": "kitna", "paisa": "paise", "bhejiye": "bhejo", "bhejna": "bhejo",
    "bhejte": "bhejo", "bhejenge": "bhejo", "karu": "karna", "karoge": "karna",
    "chaiye": "chahiye", "kimat": "daam", "keemat": "daam",
}

_CLAUSE_SPLIT = re.compile(
    r"\s+(?:and|but|also|plus|aur|&|,\s*and|then)\s+|[,;]\s+|\?\s+", re.IGNORECASE,
)

# Words that carry no subject of their own. A clause made only of these is
# conversational filler, not an unanswered business question.
FILLER = frozenset({
    "the", "a", "an", "is", "are", "am", "was", "were", "be", "been", "do",
    "does", "did", "can", "could", "would", "will", "shall", "should", "may",
    "might", "have", "has", "had", "i", "me", "my", "we", "us", "our", "you",
    "your", "yours", "it", "its", "this", "that", "these", "those", "there",
    "here", "what", "whats", "which", "who", "whom", "when", "where", "why",
    "how", "please", "pls", "kindly", "just", "some", "any", "all", "to", "of",
    "in", "on", "at", "for", "from", "with", "about", "into", "over", "so",
    "if", "then", "than", "as", "by", "or", "and", "but", "not", "no", "yes",
    "hi", "hello", "hey", "namaste", "thanks", "thank", "ok", "okay", "sir",
    "maam", "madam", "ji", "kya", "hai", "hain", "ho", "he", "ka", "ki", "ke",
    "me", "mein", "aap", "aapka", "mujhe", "mujhko", "bhi", "toh", "tell",
    "iska", "uska", "iske", "unka", "yeh", "woh", "sab", "koi", "hota",
    "hoti", "karna", "kare", "karo", "kar", "kaise", "chahiye", "batao",
    "bata", "dena", "raha", "rahe", "sakta", "sakte",
    "roughly", "approximately", "around", "work", "works", "once", "arrives",
    "arrive", "stay", "box", "boxes", "bengali", "camellia", "artisanal",
    "currently", "usually", "normally", "actually", "maybe",
    "make", "makes", "made", "prepare", "prepares", "sell", "sells",
    "offer", "offers", "available",
    "know", "want", "need", "like", "get", "give", "share", "send", "show",
    "let", "see", "look", "looking", "love", "would", "wanted", "possible",
    "much", "many", "very", "really", "still", "again", "back", "now", "today",
    "one", "two", "three", "us", "guys", "team", "there",
    # Hindi interrogatives and verb-form variants alongside their English
    # counterparts already above ("how"/"what"/"kaise" and "kar"/"sakta").
    # One member of a family already being filler is the reason its siblings
    # belong here too, not a one-off sentence fix.
    "kaisi", "kaisa", "sakti", "karte", "karti", "karta", "bare", "aaj",
    # Hedge phrasing that wraps a real question without narrowing it —
    # "the best way to X", "what sort of X", "give me an idea of X", "are you
    # able to X" — appears across every intent family, not one message.
    "able", "bring", "across", "come", "out", "far", "best", "way",
    "sort", "idea", "days", "kal", "collection",
    # Normalization folds "bhejte"/"bhejna"/"bhejte" to "bhejo" (see
    # _SPELLING), so a delivery phrase written with the original verb form
    # never matches its own now-rewritten phrase list; the fallback verb
    # regex already reads "bhejo" directly, so it needs no separate mapping
    # here — this only keeps it from counting as an unread word.
    "bhejo",
    # "order" is deliberately NOT filler. A bare "order" left unmapped is
    # what keeps "I want to order 2 veg bento" — an order-placement attempt
    # with a quantity — deferred to the deterministic order-slot/cart flow
    # instead of being answered as a generic product question. That flow is
    # state-owned by design; the word staying "unread" here is the mechanism
    # that protects it, not an oversight.
})

# --- concepts --------------------------------------------------------------
# Each entry is (single words, multi-word phrases). A concept fires when any of
# its words or phrases appears in the clause.

CONCEPTS: Dict[str, Tuple[Set[str], Tuple[str, ...]]] = {
    "menu_request": (
        {"menu", "carte", "catalogue", "catalog", "listing", "options",
         "choices", "offerings", "selection", "range"},
        ("what do you have",
         "what can i order", "what can i get", "what can i buy",
         "list of items", "your list", "what choices", "what options",
         "on your menu", "menu card", "this week", "weekly menu",
         "what you offer", "what do you offer", "kya hai menu",
         "menu bhejo", "menu dikhao"),
    ),
    "product_information": (
        {"sweets", "sweet", "mishti", "sondesh", "dessert", "desserts",
         "products", "product", "items", "item", "speciality", "specialty",
         "bento", "jolbhora", "kanchagolla", "mahua", "flavour", "flavours",
         "flavor", "flavors"},
        ("what do you sell", "what are you selling", "what you sell",
         "what do you make", "what you make", "what do you prepare",
         "tell me about", "more about", "know more",
         "difference between collection", "collection 1", "collection 2",
         "compare collection", "collections differ"),
    ),
    "product_price": (
        # "charge" is deliberately absent: it is as often a phone charger or a
        # delivery charge as it is a price, and both have better homes.
        {"price", "prices", "pricing", "cost", "costs", "rate", "rates",
         "amount", "expensive", "mrp", "kitna", "paise", "padega", "daam",
         "rupees", "money"},
        ("how much", "set me back", "price wise", "price-wise", "what does it cost",
         "how expensive", "kitna hai", "kitna padega", "kya rate"),
    ),
    "delivery_query": (
        {"deliver", "delivery", "delivers", "delivering", "ship", "shipping",
         "courier", "shipped", "pahunchate", "pahunchao"},
        ("do you deliver", "delivery hota", "bhejte ho", "bhejo ge",
         "delivery available", "deliver to", "send to"),
    ),
    "order_process": (
        {"buy", "buying", "purchase", "purchasing", "ordering", "process",
         "book", "booking"},
        ("how do i order", "how to order", "how can i order", "place an order",
         "placing an order", "place order", "order kaise", "kaise order",
         "order karna", "how do i buy", "how would i", "walk me through",
         "go about", "ordering process", "procedure for", "how does it work",
         "pre-book", "pre book", "prebook", "pre-order", "pre order"),
    ),
    "storage_question": (
        {"store", "storage", "storing", "keep", "keeping", "fridge",
         "refrigerate", "refrigerated", "refrigeration", "freezer", "freeze",
         "expiry", "expires"},
        ("shelf life", "how long does it last", "how long will it last",
         "how long will it stay", "stay good", "stays good", "room temperature"),
    ),
    "packaging_question": (
        {"packaging", "pack", "packed", "packing", "wrap", "wrapped", "wrapping",
         "cornstarch"},
        ("gift wrap", "gift wrapping", "gift-wrap", "food safe", "food-safe",
         "plastic free", "plastic-free", "eco friendly", "eco-friendly",
         "environmentally friendly", "box material", "decorate the box",
         "birthday wrapping", "custom box", "custom boxes"),
    ),
    "dietary_question": (
        {"vegan", "eggless", "jain", "diabetic"},
        ("gluten free", "gluten-free", "dairy free", "sugar free", "sugar-free",
         "suitable for vegetarians", "is it vegetarian", "no eggs"),
    ),
    "allergen_question": (
        # "gluten free" belongs to the dietary answer, which lists vegetarian,
        # eggless, gluten and vegan together — so it is deliberately not here.
        {"allergy", "allergic", "allergies", "allergen", "allergens", "nut",
         "nuts", "almond", "almonds", "cashew", "cashews", "peanut", "peanuts",
         "pistachio", "pistachios"},
        ("nut free", "nut-free"),
    ),
    "ingredients_question": (
        {"ingredient", "ingredients", "contains", "contain"},
        ("made of", "made from", "made with", "what is in it", "whats in it"),
    ),
    "payment_question": (
        {"payment", "pay", "upi", "cod", "gpay", "paytm", "phonepe"},
        ("cash on delivery", "how do i pay", "payment method", "payment options",
         "net banking", "credit card", "debit card"),
    ),
    "contact_question": (
        {"contact", "number"},
        ("phone number", "your number", "aapka number", "email", "speak to", "talk to",
         "get in touch", "who is the owner", "who is the manager",
         "who is the founder", "who is the chef", "reach you"),
    ),
    "availability_query": (
        {"availability"},
        ("available today", "available right now", "available now", "in stock",
         "out of stock", "currently available", "do you have it today"),
    ),
    "discount_request": (
        {"discount", "discounts", "coupon", "promo", "voucher"},
        ("percent off", "% off", "free delivery", "complimentary delivery",
         "free shipping", "any offer on", "best price"),
    ),
    # An occasion named as context ("sweets for my parents' anniversary") is
    # not the same as asking us to quote an event order. These fire only
    # alongside an order-shaped ask, which `_ORDER_SHAPED` decides.
    "event_order": (
        {"wedding", "party", "function", "reception", "anniversary"},
        ("for an event", "for our event", "for a wedding", "for a party"),
    ),
    "bulk_order": (
        {"bulk", "corporate", "festive", "wholesale"},
        ("for our company", "for the office", "gifting for", "large order"),
    ),
    "subscription_query": (
        {"subscription", "subscriptions", "supscription", "tiffin"},
        ("meal plan", "meal package", "everyday meal", "weekly meal",
         "monthly meal", "meal subscription"),
    ),
    "pack_size_question": (
        {"pieces", "pcs"},
        ("how many pieces", "how many in a box", "piece count"),
    ),
}

# Words that make a subscription clause about booking or changing rather than
# about the plans themselves.
# The ask that turns an occasion into an order a person has to quote.
_ORDER_SHAPED = re.compile(
    r"\b(need|needed|order|ordering|arrange|arranging|quote|book|booking|"
    r"plan|planning|cater|catering|supply|require|looking for|help with|"
    r"can you do|interested in)\b",
    re.IGNORECASE,
)

_SUBSCRIPTION_BOOKING = re.compile(
    r"\b(book|booking|subscribe|sign me up|start|next steps?|join)\b", re.IGNORECASE,
)
_SUBSCRIPTION_CHANGE = re.compile(
    r"\b(change|changing|reschedul\w*|postpone|prepone|shift|skip|pause|cancel)\b",
    re.IGNORECASE,
)

# --- entities --------------------------------------------------------------

# Most specific first: a flavour beats the collection it sits in, and both
# beat the generic word for sweets.
PRODUCTS = (
    ("jolbhora", "Jolbhora"), ("kanchagolla", "Kanchagolla"),
    ("kancha golla", "Kanchagolla"), ("talgash", "Jolbhora"), ("mahua", "Mahua"),
    ("rose", "Rose"), ("kesar", "Kesar"), ("mango", "Mango"),
    ("vanilla", "Vanilla"), ("dalchini", "Dalchini"), ("classic", "Classic"),
    ("nolen gur", "Nolen Gur"), ("bento", "Bento"),
    ("aamar mishti", "Aamar Mishti"), ("aamar", "Aamar Mishti"),
    ("signature", "Signature"),
    # The client names the two ranges "Collection 1" and "Collection 2"; those
    # are the same products as Aamar Mishti and Signature.
    ("collection 1", "Aamar Mishti"), ("collection1", "Aamar Mishti"),
    ("collection 2", "Signature"), ("collection2", "Signature"),
    ("sondesh", "Sondesh"), ("mishti", "Sondesh"), ("sweets", "Sondesh"),
)

# The generic word for the whole range: "sweets menu" is a menu request, while
# "collection 1 menu" is that collection's own card.
GENERIC_PRODUCTS = frozenset({"Sondesh"})

LOCATIONS = (
    "delhi", "new delhi", "gurgaon", "noida", "greater noida", "ghaziabad",
    "faridabad", "dwarka", "sushant lok", "chatra marg", "mumbai", "pune",
    "bangalore", "bengaluru", "kolkata", "hyderabad", "chennai", "jaipur",
    "ahmedabad", "chandigarh", "lucknow", "ncr",
)

# Subtopics worth separating from their domain, because a domain answer does
# not imply them. The right-hand side is what the approved copy would have to
# mention for the subtopic to count as covered.
# Which domain each subtopic belongs to, so a gift-wrap question narrows the
# packaging answer and leaves a price answer in the same message alone.
SUBTOPIC_DOMAIN: Dict[str, str] = {
    "alacarte": "ordering",
    "gift_wrap": "packaging",
    "custom_box": "packaging",
    "same_day": "delivery",
    "delivery_fee": "delivery",
    "cancellation": "subscriptions",
    "rescheduling": "subscriptions",
    "collection_difference": "products",
}

SUBTOPICS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "alacarte": (
        ("item from menu", "items from the menu", "from the menu", "a la carte",
         "à la carte", "alacarte", "pre-book", "pre book", "prebook",
         "pre-order", "pre order", "order a dish", "order from this week"),
        # The à la carte steps ask for the slot and the quantity of items,
        # which is what makes them the pre-booking answer.
        ("choice of items", "which slot", "quantity"),
    ),
    "gift_wrap": (
        ("gift wrap", "gift-wrap", "gift wrapping", "giftwrap", "wrapping",
         "decorate the box", "decorated box", "ribbon", "birthday wrapping"),
        ("gift wrap", "gift-wrap", "wrapping", "ribbon"),
    ),
    "collection_difference": (
        ("difference between collection", "collection 1 and collection 2",
         "collection 1 vs collection 2", "compare collection",
         "collections differ", "difference between the collections"),
        ("collection 1", "collection 2"),
    ),
    "custom_box": (
        ("custom box", "custom boxes", "customised box", "customized box",
         "personalised box", "personalized box", "your own box"),
        ("custom box", "customis", "customiz", "personalis", "personaliz"),
    ),
    "same_day": (
        ("same day", "same-day", "within 30 minutes", "in 30 minutes",
         "in an hour", "right now delivery", "immediate delivery"),
        ("same day", "same-day", "30 minutes", "within an hour"),
    ),
    "delivery_fee": (
        ("delivery fee", "delivery charge", "delivery charges", "shipping cost",
         "courier charge", "courier charges", "how much is delivery",
         "the charges", "any charges", "extra charges", "charges"),
        ("charge", "charges", "actuals", "fee"),
    ),
    "cancellation": (
        ("cancel my subscription", "cancel the subscription", "cancellation policy",
         "cancel my order", "refund policy"),
        ("cancel", "cancellation", "refund"),
    ),
    "rescheduling": (
        ("change a delivery date", "change my delivery date", "reschedule",
         "postpone", "prepone", "change the date"),
        ("change a delivery date", "5 hours in advance", "reschedul"),
    ),
}


# What a customer is asking about comes before what we cannot answer, so a
# compound reply reads in the order the meanings were raised rather than
# alphabetically.
_INTENT_ORDER = (
    "greeting", "menu_request", "product_information", "product_price",
    "pack_size_question", "delivery_query", "order_process", "storage_question",
    "ingredients_question", "allergen_question", "packaging_question",
    "payment_question", "contact_question", "subscription_query",
    "subscription_booking", "subscription_change", "availability_query",
    "discount_request", "event_order", "bulk_order", "unknown_business_query",
    "unknown",
)


def _intent_order(intent: str) -> int:
    return _INTENT_ORDER.index(intent) if intent in _INTENT_ORDER else len(_INTENT_ORDER)


def normalize_menu_type(value: Optional[str]) -> Optional[str]:
    """Canonical "vegetarian"/"non_vegetarian" for whatever wording named it.

    A model is asked for one of a few named values, but nothing enforces
    that at the API boundary — it can say "veg" where the catalogue matches
    on "vegetarian". An unnormalized value fails every matcher silently and
    falls through to the generic menu overview. This is the single place
    either path (model or offline reader) turns wording into the value the
    catalogue actually matches on.
    """
    text = (value or "").lower().replace("-", " ").replace("_", " ")
    if not text.strip():
        return None
    if re.search(r"\bnon\s*veg\w*\b", text) or re.search(r"\b(chicken|mutton|fish)\b", text):
        return "non_vegetarian"
    if re.search(r"\b(veg\w*|niramish)\b", text):
        return "vegetarian"
    return None


def normalize_pack_size(value: Optional[str]) -> Optional[str]:
    """Canonical "250g"/"500g"/"1kg" for whatever wording named a size.

    The knowledge-record matchers do substring containment ("250" in value),
    so a model returning "250 grams" already matches — this only closes the
    cases substring matching cannot reach: word forms like "half kilo".
    """
    text = (value or "").lower().strip()
    if not text:
        return None
    if re.search(r"\bquarter\s*kilo\b", text) or "250" in text:
        return "250g"
    if re.search(r"\bhalf\s*kilo\b", text) or "500" in text:
        return "500g"
    if re.search(r"\b1\s*kg\b|\bone\s*kg\b", text) or "1000" in text or "1kg" in text:
        return "1kg"
    return text or None


def _normalise(message: str) -> str:
    text = " ".join((message or "").lower().split())
    text = re.sub(r"[^\w%₹\s\-/]", " ", text)
    text = " ".join(_SPELLING.get(word, word) for word in text.split())
    return " ".join(text.split())


def _clauses(message: str) -> List[str]:
    parts = [part.strip() for part in _CLAUSE_SPLIT.split(message or "") if part and part.strip()]
    return parts or [(message or "").strip()]


def _concepts_in(clause: str) -> Set[str]:
    words = set(clause.split())
    found = set()
    for intent, (tokens, phrases) in CONCEPTS.items():
        if words & tokens or any(phrase in clause for phrase in phrases):
            found.add(intent)
    return found


def _subtopic_in(clause: str) -> Optional[str]:
    for name, (surface_forms, _evidence) in SUBTOPICS.items():
        if any(form in clause for form in surface_forms):
            return name
    return None


def subtopic_domain(subtopic: str) -> Optional[str]:
    """The domain a subtopic narrows, or None if it narrows nothing known."""
    return SUBTOPIC_DOMAIN.get(subtopic)


def subtopic_evidence(subtopic: str) -> Tuple[str, ...]:
    """What approved copy must mention for this subtopic to be covered."""
    entry = SUBTOPICS.get(subtopic)
    return entry[1] if entry else ()


def _entities_in(clause: str, whole: str) -> Dict[str, Any]:
    entities: Dict[str, Any] = {}
    for needle, value in PRODUCTS:
        if needle in clause:
            entities["product"] = value
            break
    for location in sorted(LOCATIONS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(location)}\b", clause):
            entities["location"] = location.title()
            break
    menu_type = normalize_menu_type(clause)
    if menu_type:
        entities["menu_type"] = menu_type
    pack_size = re.search(r"\b(250\s?gm?s?|500\s?gm?s?|1\s?kgs?)\b", clause)
    if pack_size:
        entities["pack_size"] = normalize_pack_size(pack_size.group(1)) or pack_size.group(1).replace(" ", "")
    quantity = re.search(r"\b(\d{1,5})\s*(boxes|box|packs?|kg|pieces|pcs)\b", clause)
    if quantity:
        entities["quantity"] = int(quantity.group(1))
    event = re.search(r"\b(wedding|party|function|reception|anniversary|event)\b", clause)
    if event:
        entities["event"] = event.group(1)
    subtopic = _subtopic_in(clause)
    if subtopic:
        entities["subtopic"] = subtopic
    return entities


def _meaningful_words(clause: str) -> List[str]:
    # Two letters is enough to name a subject ("ac", "wc"), and anything
    # shorter than that is noise.
    return [word for word in clause.split() if word not in FILLER and len(word) >= 2]


def _mapped_words(clause: str, found: Set[str], entities: Dict[str, Any]) -> Set[str]:
    """Every word the reader can account for in this clause."""
    mapped: Set[str] = set(FILLER)
    for intent in found:
        tokens, phrases = CONCEPTS.get(intent, (set(), ()))
        mapped |= {word for word in tokens if word in clause}
        for phrase in phrases:
            if phrase in clause:
                mapped |= set(phrase.split())
    for value in entities.values():
        mapped |= set(str(value).lower().split())
    # The words that produced an entity are accounted for too, even when the
    # entity is stored under its canonical name ("mishti" -> Sondesh).
    for needle, _value in PRODUCTS:
        if needle in clause:
            mapped |= set(needle.split())
    for location in LOCATIONS:
        if location in clause:
            mapped |= set(location.split())
    if entities.get("menu_type"):
        mapped |= {"veg", "non", "vegetarian", "niramish", "chicken", "mutton", "fish"}
    if entities.get("pack_size"):
        mapped |= {entities["pack_size"], "gm", "gms", "kg"}
    for name, (surface_forms, _evidence) in SUBTOPICS.items():
        if entities.get("subtopic") == name:
            for form in surface_forms:
                if form in clause:
                    mapped |= set(form.split())
    return mapped


# Physical-space and amenity words this business has no vocabulary for at
# all — wheelchairs, parking, Wi-Fi, washrooms, table bookings, air
# conditioning, smoking, phone charging, photography. A clause naming one of
# these always means that, whatever domain word happens to sit beside it:
# "is the delivery van wheelchair accessible" is a wheelchair-access question
# wearing a delivery word, not a delivery question. Without this, the same
# keyword-presence reading that lets "menu bhejo" mean menu_request also lets
# an unrelated amenity question borrow whichever domain word it mentions in
# passing. This exact word list is also what responder.OUT_OF_SCOPE_TOPIC_RE
# refuses on the deterministic path (imported from here, not duplicated), so
# a message this reader overrides can never fall through to legacy trigger
# matching, which has no notion of the override and would re-score the same
# borrowed word from scratch.
AMENITY_TOPIC_PATTERN = (
    r"valet|parking|wi-?fi|dine[- ]in|dining in|seating|sit[- ]in|"
    r"wheelchair|accessib\w*|washroom|toilet|restroom|"
    r"table (?:booking|reservation)|reserve a table|book(?:ing)? a table|"
    r"air.?condition\w*|smoking|charging point|charge my phone|"
    r"photograph\w*|dress code|loyalty (?:card|program)|lift accessible"
)
_AMENITY_OVERRIDE_RE = re.compile(rf"\b(?:{AMENITY_TOPIC_PATTERN})\b", re.IGNORECASE)


def read(message: str) -> Dict[str, Any]:
    """Return validated intents and entities for a message, clause by clause.

    The shape matches what the model returns, so everything downstream —
    knowledge resolution, sufficiency, actions — is identical either way.
    """
    normalised = _normalise(message)
    intents: List[str] = []
    entities: Dict[str, Any] = {}
    unknown_topics: List[str] = []

    unmapped: List[str] = []
    for clause in _clauses(normalised):
        amenity_override = _AMENITY_OVERRIDE_RE.search(clause)
        found = set() if amenity_override else _concepts_in(clause)
        # Every concept the clause matched, including the ones precedence
        # rules drop below — their words are still read, so they must not
        # count as terms the reader failed to account for.
        matched_concepts = set(found)
        clause_entities = _entities_in(clause, normalised)
        clause_accounted = False

        # A price question that also names a product is one meaning, not two.
        if "product_price" in found:
            found.discard("product_information")
        if "menu_request" in found and "product_information" in found:
            # "collection 1 options" and "which sweets do you have" both name a
            # product: the customer wants that product's own answer unless the
            # word menu is actually there.
            named_product = clause_entities.get("product") not in (None, *GENERIC_PRODUCTS)
            if named_product:
                found.discard("menu_request")
            else:
                found.discard("product_information")
        if found & {"storage_question", "allergen_question", "ingredients_question",
                    "packaging_question", "payment_question", "pack_size_question"}:
            found.discard("product_information")
        if "allergen_question" in found:
            found.discard("ingredients_question")
        if "subscription_query" in found:
            found -= {"menu_request", "product_information"}
            if _SUBSCRIPTION_CHANGE.search(clause):
                found = (found - {"subscription_query"}) | {"subscription_change"}
            elif _SUBSCRIPTION_BOOKING.search(clause):
                found = (found - {"subscription_query"}) | {"subscription_booking"}
        if "order_process" in found:
            found.discard("menu_request")
            # Booking a subscription is the subscription's own step list.
            if found & {"subscription_query", "subscription_booking", "subscription_change"}:
                found.discard("order_process")
        # A size on its own asks what comes in that box; a size next to a
        # product name is that product in that size, which the product answer
        # and the order draft both handle better.
        if (
            clause_entities.get("pack_size")
            and "menu" not in clause
            and not clause_entities.get("product")
        ):
            found.add("pack_size_question")
            found.discard("menu_request")
            found.discard("product_information")
        if "dietary_question" in found:
            found -= {"allergen_question", "ingredients_question",
                      "menu_request", "product_information"}
        if "payment_question" in found and re.search(r"\b(cod|cash on delivery)\b", clause):
            found.discard("delivery_query")
        if "discount_request" in found and "free delivery" in clause:
            found.discard("delivery_query")
        if found & {"event_order", "bulk_order"} and not (
            _ORDER_SHAPED.search(clause) or clause_entities.get("quantity")
        ):
            found -= {"event_order", "bulk_order"}
        if "event_order" in found:
            found.discard("bulk_order")
        # A location on its own is not a delivery question ("I am in Noida"),
        # but any verb of bringing plus a location is.
        if not found and clause_entities.get("location") and re.search(
            r"\b(bhejo|deliver|delivery|send|bring|drop|come|reach|ship)\b", clause,
        ):
            found.add("delivery_query")
        # A clause that names something but matches no concept still carries
        # its meaning in the entity: a product is a product question, a menu
        # type is a menu request, a pack size is a pack-size question, and a
        # subtopic belongs to the domain it narrows.
        if not found:
            if clause_entities.get("product"):
                found.add("product_information")
            elif clause_entities.get("pack_size"):
                found.add("pack_size_question")
            elif clause_entities.get("menu_type"):
                found.add("menu_request")
            elif clause_entities.get("subtopic"):
                found |= {
                    "packaging": {"packaging_question"},
                    "delivery": {"delivery_query"},
                    "subscriptions": {"subscription_query"},
                    "products": {"product_information"},
                }.get(SUBTOPIC_DOMAIN.get(clause_entities["subtopic"], ""), set())

        if found:
            intents.extend(sorted(found, key=_intent_order))
        else:
            meaningful = _meaningful_words(clause)
            # A clause with a real subject that matches no concept is a
            # business question we have no reading for — typed, not dropped.
            if meaningful:
                unknown_topics.append(" ".join(meaningful[:6]))
                # The clause is accounted for — as a gap. Its words are not
                # evidence that the reader missed something else.
                clause_accounted = True

        # Words the reader could not account for mean it read the clause only
        # partly. It still answers what it understood, but it does not claim
        # the message: something more specific may be meant by the rest.
        mapped = _mapped_words(clause, matched_concepts | found, clause_entities)
        if not clause_accounted:
            unmapped.extend(
                word for word in clause.split()
                if word not in mapped and len(word) > 2 and not word.isdigit()
            )

        for key, value in clause_entities.items():
            entities.setdefault(key, value)

    quantity = entities.get("quantity")
    if isinstance(quantity, int) and quantity >= 20 and "bulk_order" not in intents:
        if "event_order" not in intents:
            intents.append("bulk_order")

    if unknown_topics:
        intents.append("unknown_business_query")
        entities.setdefault("topic", unknown_topics[0])

    # A change question anywhere in a subscription message is a change
    # question about the subscription, not a separate delivery query.
    if entities.get("subtopic") in {"rescheduling", "cancellation"} and any(
        intent.startswith("subscription") for intent in intents
    ):
        intents = [
            intent for intent in intents
            if not intent.startswith("subscription") and intent != "delivery_query"
        ] + ["subscription_change"]

    ordered = list(dict.fromkeys(intents))
    if not ordered:
        ordered = ["unknown"]
    return {
        "intents": ordered,
        "entities": entities,
        "unknown_topics": unknown_topics,
        "unmapped": list(dict.fromkeys(unmapped)),
    }
