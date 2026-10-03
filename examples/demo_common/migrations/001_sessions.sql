CREATE TABLE commerce_sessions (
    session_id text PRIMARY KEY,
    version bigint NOT NULL CHECK (version > 0),
    document jsonb NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    messages jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(messages) = 'array')
);

CREATE INDEX commerce_sessions_user ON commerce_sessions ((document->>'user_id'));
