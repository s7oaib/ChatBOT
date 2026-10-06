"""Additive exact-Story route references; never store copied answers."""
from datetime import datetime, timezone

from camellia import approved_qa


def initialize(cursor):
    cursor.execute('''CREATE TABLE IF NOT EXISTS instagram_story_qa_bundles (
        routing_id INTEGER PRIMARY KEY, context TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by TEXT NOT NULL, updated_by TEXT NOT NULL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS instagram_story_qa_bundle_items (
        routing_id INTEGER NOT NULL, qa_id TEXT NOT NULL, position INTEGER NOT NULL,
        created_at TEXT NOT NULL, created_by TEXT NOT NULL,
        PRIMARY KEY(routing_id,qa_id), UNIQUE(routing_id,position))''')
    # Triggers cover every existing hard-delete/lifecycle path, without relying
    # on connection-specific SQLite foreign_keys settings or rewriting routes.
    cursor.execute('''CREATE TRIGGER IF NOT EXISTS story_qa_route_deleted
        AFTER DELETE ON instagram_source_routes BEGIN
        DELETE FROM instagram_story_qa_bundle_items WHERE routing_id=OLD.routing_id;
        DELETE FROM instagram_story_qa_bundles WHERE routing_id=OLD.routing_id;
        END''')
    cursor.execute('''CREATE TRIGGER IF NOT EXISTS story_qa_identity_guard
        BEFORE UPDATE OF source_type,media_id ON instagram_source_routes
        WHEN EXISTS(SELECT 1 FROM instagram_story_qa_bundles WHERE routing_id=OLD.routing_id)
        AND (NEW.source_type IS NOT OLD.source_type OR NEW.media_id IS NOT OLD.media_id)
        BEGIN SELECT RAISE(ABORT,'Story bundle identity cannot change'); END''')


def read(conn, route):
    if not route or route['source_type'] != 'story':
        return None
    bundle = conn.execute('SELECT * FROM instagram_story_qa_bundles WHERE routing_id=?', (route['routing_id'],)).fetchone()
    ids = [row['qa_id'] for row in conn.execute(
        'SELECT qa_id FROM instagram_story_qa_bundle_items WHERE routing_id=? ORDER BY position',
        (route['routing_id'],))] if bundle else (
        [route['approved_qa_id']] if route['response_mode'] == 'APPROVED_QA' and route['approved_qa_id'] else [])
    return {'story_bundle': bool(bundle), 'story_context': bundle['context'] if bundle else None,
            'approved_qa_ids': ids, 'approved_qa_id': route['approved_qa_id']}


def validate(context, qa_ids, rows):
    if not isinstance(context, str) or context not in {item['value'] for item in approved_qa.context_options(rows)}:
        raise ValueError('Choose an allowed Approved Q&A context')
    if (not isinstance(qa_ids, list) or not 1 <= len(qa_ids) <= 50
            or any(not isinstance(key, str) or not key or len(key) > 100 for key in qa_ids)):
        raise ValueError('Select 1 to 50 stable Approved Q&A IDs')
    if len(set(qa_ids)) != len(qa_ids):
        raise ValueError('Duplicate Approved Q&A IDs are not allowed')
    effective = {entry.qa_id: entry for entry in approved_qa.registry(rows)}
    if any(key not in effective for key in qa_ids):
        raise ValueError('Every selected Q&A must be effective')
    if any(effective[key].context not in {context, 'general'} for key in qa_ids):
        raise ValueError('Selected Q&A must belong to the exact context or explicit General group')
    return effective


def replace(conn, routing_id, story_id, context, qa_ids, actor):
    """Caller owns one BEGIN IMMEDIATE transaction and the QA publication lock."""
    route = conn.execute('SELECT * FROM instagram_source_routes WHERE routing_id=?', (routing_id,)).fetchone()
    if (not route or route['source_type'] != 'story' or route['media_id'] != story_id
            or route['response_mode'] != 'APPROVED_QA' or route['archived_at']):
        raise ValueError('An unchanged exact Story Approved Q&A route is required')
    validate(context, qa_ids, approved_qa._read(conn))
    now = datetime.now(timezone.utc).isoformat()
    conn.execute('''INSERT INTO instagram_story_qa_bundles
        (routing_id,context,created_at,updated_at,created_by,updated_by) VALUES (?,?,?,?,?,?)
        ON CONFLICT(routing_id) DO UPDATE SET context=excluded.context,
        updated_at=excluded.updated_at,updated_by=excluded.updated_by''',
        (routing_id, context, now, now, actor, actor))
    conn.execute('DELETE FROM instagram_story_qa_bundle_items WHERE routing_id=?', (routing_id,))
    conn.executemany('''INSERT INTO instagram_story_qa_bundle_items
        (routing_id,qa_id,position,created_at,created_by) VALUES (?,?,?,?,?)''',
        [(routing_id, key, position, now, actor) for position, key in enumerate(qa_ids)])
    primary = route['approved_qa_id'] if route['approved_qa_id'] in qa_ids else qa_ids[0]
    conn.execute('UPDATE instagram_source_routes SET approved_qa_id=? WHERE routing_id=?', (primary, routing_id))


def diagnostic(database):
    with database.get_connection() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {'schema_initialized': {'instagram_story_qa_bundles', 'instagram_story_qa_bundle_items'} <= tables,
                'stored_bundles': conn.execute('SELECT COUNT(*) FROM instagram_story_qa_bundles').fetchone()[0],
                'stored_items': conn.execute('SELECT COUNT(*) FROM instagram_story_qa_bundle_items').fetchone()[0]}
