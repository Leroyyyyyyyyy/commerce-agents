CREATE TABLE retail_carts (
    session_id text PRIMARY KEY REFERENCES commerce_sessions(session_id) ON DELETE CASCADE,
    max_quantity integer NOT NULL CHECK (max_quantity > 0),
    max_lines integer NOT NULL CHECK (max_lines > 0),
    UNIQUE (session_id, max_quantity)
);

CREATE TABLE retail_cart_items (
    session_id text NOT NULL,
    product_id text NOT NULL,
    quantity integer NOT NULL,
    max_quantity integer NOT NULL,
    product jsonb NOT NULL CHECK (jsonb_typeof(product) = 'object'),
    PRIMARY KEY (session_id, product_id),
    FOREIGN KEY (session_id, max_quantity)
        REFERENCES retail_carts(session_id, max_quantity) ON DELETE CASCADE,
    CHECK (quantity >= 1 AND quantity <= max_quantity)
);
