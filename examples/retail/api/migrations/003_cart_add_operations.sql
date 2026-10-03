CREATE TABLE retail_cart_add_operations (
    session_id text NOT NULL REFERENCES commerce_sessions(session_id) ON DELETE CASCADE,
    operation_id uuid NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    status integer NOT NULL CHECK (status IN (200, 400)),
    response jsonb NOT NULL CHECK (jsonb_typeof(response) = 'object'),
    PRIMARY KEY (session_id, operation_id)
);
