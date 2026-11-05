-- Run after the backend has initialized kokwet_app.live_messages.
-- The public Realtime projection deliberately excludes hidden flags,
-- client hashes, and voice-note binary data.

CREATE TABLE IF NOT EXISTS kokwet_app.live_message_feed (
    message_id TEXT PRIMARY KEY,
    session_id TEXT,
    channel TEXT NOT NULL CHECK (channel IN ('topic', 'public')),
    author_name TEXT NOT NULL,
    message_text TEXT NOT NULL,
    message_type TEXT NOT NULL CHECK (message_type IN ('text', 'voice', 'tag')),
    tag_name TEXT,
    created_at TEXT NOT NULL
);

ALTER TABLE kokwet_app.live_message_feed REPLICA IDENTITY FULL;
ALTER TABLE kokwet_app.live_message_feed ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Anyone can read the live message feed"
    ON kokwet_app.live_message_feed;
CREATE POLICY "Anyone can read the live message feed"
    ON kokwet_app.live_message_feed
    FOR SELECT
    TO anon, authenticated
    USING (true);

REVOKE ALL ON TABLE kokwet_app.live_message_feed FROM PUBLIC;
REVOKE ALL ON TABLE kokwet_app.live_message_feed FROM anon, authenticated;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA kokwet_app
    FROM PUBLIC, anon, authenticated;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA kokwet_app
    FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA kokwet_app
    REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA kokwet_app
    REVOKE ALL ON SEQUENCES FROM PUBLIC, anon, authenticated;
GRANT SELECT ON TABLE kokwet_app.live_message_feed TO anon, authenticated;
GRANT USAGE ON SCHEMA kokwet_app TO anon, authenticated;

CREATE OR REPLACE FUNCTION kokwet_app.sync_live_message_feed()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, kokwet_app
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM kokwet_app.live_message_feed
        WHERE message_id = OLD.message_id;
        RETURN OLD;
    END IF;

    IF NEW.is_hidden THEN
        DELETE FROM kokwet_app.live_message_feed
        WHERE message_id = NEW.message_id;
        RETURN NEW;
    END IF;

    INSERT INTO kokwet_app.live_message_feed (
        message_id,
        session_id,
        channel,
        author_name,
        message_text,
        message_type,
        tag_name,
        created_at
    )
    VALUES (
        NEW.message_id,
        NEW.session_id,
        NEW.channel,
        NEW.author_name,
        NEW.message_text,
        NEW.message_type,
        NEW.tag_name,
        NEW.created_at
    )
    ON CONFLICT (message_id) DO UPDATE SET
        session_id = EXCLUDED.session_id,
        channel = EXCLUDED.channel,
        author_name = EXCLUDED.author_name,
        message_text = EXCLUDED.message_text,
        message_type = EXCLUDED.message_type,
        tag_name = EXCLUDED.tag_name,
        created_at = EXCLUDED.created_at;

    RETURN NEW;
END;
$function$;

REVOKE ALL ON FUNCTION kokwet_app.sync_live_message_feed() FROM PUBLIC;
REVOKE ALL ON FUNCTION kokwet_app.sync_live_message_feed() FROM anon, authenticated;

DROP TRIGGER IF EXISTS live_messages_sync_realtime_feed
    ON kokwet_app.live_messages;
CREATE TRIGGER live_messages_sync_realtime_feed
    AFTER INSERT OR UPDATE OR DELETE
    ON kokwet_app.live_messages
    FOR EACH ROW
    EXECUTE FUNCTION kokwet_app.sync_live_message_feed();

INSERT INTO kokwet_app.live_message_feed (
    message_id,
    session_id,
    channel,
    author_name,
    message_text,
    message_type,
    tag_name,
    created_at
)
SELECT
    message_id,
    session_id,
    channel,
    author_name,
    message_text,
    message_type,
    tag_name,
    created_at
FROM kokwet_app.live_messages
WHERE is_hidden = FALSE
ON CONFLICT (message_id) DO UPDATE SET
    session_id = EXCLUDED.session_id,
    channel = EXCLUDED.channel,
    author_name = EXCLUDED.author_name,
    message_text = EXCLUDED.message_text,
    message_type = EXCLUDED.message_type,
    tag_name = EXCLUDED.tag_name,
    created_at = EXCLUDED.created_at;

DO $block$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_publication
        WHERE pubname = 'supabase_realtime'
    ) AND NOT EXISTS (
        SELECT 1
        FROM pg_publication_tables
        WHERE pubname = 'supabase_realtime'
          AND schemaname = 'kokwet_app'
          AND tablename = 'live_message_feed'
    ) THEN
        ALTER PUBLICATION supabase_realtime
        ADD TABLE kokwet_app.live_message_feed;
    END IF;
END;
$block$;
