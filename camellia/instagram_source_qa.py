"""Additive Post/Reel references; accepted Story storage stays intact."""
from datetime import datetime, timezone
from camellia import approved_qa, instagram_story_qa

IDENTITY_FIELDS = ('source_type', 'media_id', 'canonical_url', 'shortcode')
validate = instagram_story_qa.validate
OWNED_IDENTITY_SQL = '''SELECT 1 FROM instagram_media m
    JOIN instagram_media_inventory_syncs s ON s.owner_account_id=m.owner_instagram_account_id
    WHERE m.instagram_media_id=? AND m.permalink=? AND m.shortcode=?
    AND m.inventory_state='CURRENT'
    AND CASE WHEN m.media_type='VIDEO' THEN 'reel' ELSE 'post' END=?'''


def initialize(cursor):
    cursor.execute('''CREATE TABLE IF NOT EXISTS instagram_source_qa_bundles (
        routing_id INTEGER PRIMARY KEY, source_type TEXT NOT NULL CHECK(source_type IN ('post','reel')),
        context TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by TEXT NOT NULL, updated_by TEXT NOT NULL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS instagram_source_qa_bundle_items (
        routing_id INTEGER NOT NULL, qa_id TEXT NOT NULL, position INTEGER NOT NULL,
        created_at TEXT NOT NULL, created_by TEXT NOT NULL,
        PRIMARY KEY(routing_id,qa_id), UNIQUE(routing_id,position))''')
    cursor.execute('''CREATE TRIGGER IF NOT EXISTS source_qa_route_deleted
        AFTER DELETE ON instagram_source_routes BEGIN
        DELETE FROM instagram_source_qa_bundle_items WHERE routing_id=OLD.routing_id;
        DELETE FROM instagram_source_qa_bundles WHERE routing_id=OLD.routing_id;
        END''')
    cursor.execute('''CREATE TRIGGER IF NOT EXISTS source_qa_identity_guard
        BEFORE UPDATE OF source_type,media_id,canonical_url,shortcode ON instagram_source_routes
        WHEN EXISTS(SELECT 1 FROM instagram_source_qa_bundles WHERE routing_id=OLD.routing_id)
        AND (NEW.source_type IS NOT OLD.source_type
             OR NEW.canonical_url IS NOT OLD.canonical_url OR NEW.shortcode IS NOT OLD.shortcode
             OR (NEW.media_id IS NOT OLD.media_id AND NOT EXISTS (
                 SELECT 1 FROM instagram_post_pairings p WHERE p.routing_id=OLD.routing_id
                 AND p.status='bound' AND p.bound_media_id=NEW.media_id AND p.bound_at=NEW.updated_at
                 AND p.id=(SELECT MAX(id) FROM instagram_post_pairings WHERE routing_id=OLD.routing_id))
                 AND NOT EXISTS (SELECT 1 FROM instagram_media m
                     JOIN instagram_media_inventory_syncs s ON s.owner_account_id=m.owner_instagram_account_id
                     WHERE m.instagram_media_id=NEW.media_id AND m.permalink=OLD.canonical_url
                     AND m.shortcode=OLD.shortcode AND m.inventory_state='CURRENT'
                     AND CASE WHEN m.media_type='VIDEO' THEN 'reel' ELSE 'post' END=OLD.source_type)))
        BEGIN SELECT RAISE(ABORT,'Post/Reel bundle identity cannot change'); END''')


def identity(route):
    return {key: route[key] for key in IDENTITY_FIELDS}


def compatible_identity(conn, current, candidate):
    """Keep exact links fixed; allow existing verified owned-media enrichment."""
    if any(current[key] != candidate.get(key) for key in ('source_type', 'canonical_url', 'shortcode')):
        return False
    return current['media_id'] == candidate.get('media_id') or bool(conn.execute(
        OWNED_IDENTITY_SQL, (candidate.get('media_id'), current['canonical_url'], current['shortcode'], current['source_type'])).fetchone())


def read(conn, route):
    if not route or route['source_type'] not in {'post', 'reel'}:
        return None
    bundle = conn.execute('SELECT * FROM instagram_source_qa_bundles WHERE routing_id=?', (route['routing_id'],)).fetchone()
    ids = [row['qa_id'] for row in conn.execute(
        'SELECT qa_id FROM instagram_source_qa_bundle_items WHERE routing_id=? ORDER BY position',
        (route['routing_id'],))] if bundle else (
        [route['approved_qa_id']] if route['response_mode'] == 'APPROVED_QA' and route['approved_qa_id'] else [])
    return {'qa_bundle': bool(bundle), 'source_context': bundle['context'] if bundle else None,
            'approved_qa_ids': ids, 'approved_qa_id': route['approved_qa_id']}


def clear(conn, routing_id):
    conn.execute('DELETE FROM instagram_source_qa_bundle_items WHERE routing_id=?', (routing_id,))
    conn.execute('DELETE FROM instagram_source_qa_bundles WHERE routing_id=?', (routing_id,))


def replace(conn, routing_id, exact_identity, context, qa_ids, actor):
    """Caller owns BEGIN IMMEDIATE and the existing QA publication lock."""
    route = conn.execute('SELECT * FROM instagram_source_routes WHERE routing_id=?', (routing_id,)).fetchone()
    if (not route or route['source_type'] not in {'post', 'reel'} or identity(route) != exact_identity
            or route['response_mode'] != 'APPROVED_QA' or route['archived_at']
            or not (route['media_id'] or route['canonical_url'] or route['shortcode'])):
        raise ValueError('An unchanged exact Post/Reel Approved Q&A route is required')
    validate(context, qa_ids, approved_qa._read(conn))
    now = datetime.now(timezone.utc).isoformat()
    conn.execute('''INSERT INTO instagram_source_qa_bundles
        (routing_id,source_type,context,created_at,updated_at,created_by,updated_by) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(routing_id) DO UPDATE SET context=excluded.context,
        updated_at=excluded.updated_at,updated_by=excluded.updated_by''',
        (routing_id, route['source_type'], context, now, now, actor, actor))
    conn.execute('DELETE FROM instagram_source_qa_bundle_items WHERE routing_id=?', (routing_id,))
    conn.executemany('''INSERT INTO instagram_source_qa_bundle_items
        (routing_id,qa_id,position,created_at,created_by) VALUES (?,?,?,?,?)''',
        [(routing_id, key, position, now, actor) for position, key in enumerate(qa_ids)])
    primary = route['approved_qa_id'] if route['approved_qa_id'] in qa_ids else qa_ids[0]
    conn.execute('UPDATE instagram_source_routes SET approved_qa_id=? WHERE routing_id=?', (primary, routing_id))


def effective(database, bundle):
    return instagram_story_qa.effective(database, {**bundle, 'story_context': bundle['source_context']})


def route_contract(database, route):
    if route and route.get('source_type') in {'post', 'reel'}:
        return {**route, **(database.get_instagram_source_qa_bundle(route['routing_id']) or {})}
    return route


def diagnostic(database):
    with database.get_connection() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {'schema_initialized': {'instagram_source_qa_bundles', 'instagram_source_qa_bundle_items'} <= tables,
                'stored_bundles': conn.execute('SELECT COUNT(*) FROM instagram_source_qa_bundles').fetchone()[0],
                'stored_items': conn.execute('SELECT COUNT(*) FROM instagram_source_qa_bundle_items').fetchone()[0]}
