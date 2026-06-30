-- 決済アプリの DDL（解析サンプル用）

CREATE TABLE users (
    id BIGINT PRIMARY KEY,
    email VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE payments (
    id BIGINT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    amount INTEGER NOT NULL,
    transaction_id VARCHAR(64),
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX idx_payments_txn ON payments (transaction_id);
CREATE INDEX idx_payments_user_status ON payments (user_id, status);
