-- Fixed simulation schema. init_db validates existing objects before running DDL.
CREATE TABLE IF NOT EXISTS products (
    id BIGINT UNSIGNED NOT NULL,
    sku VARCHAR(32) NOT NULL,
    name VARCHAR(128) NOT NULL,
    category VARCHAR(64) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_products_sku (sku)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='order-query-agent simulated data v1';

CREATE TABLE IF NOT EXISTS orders (
    id BIGINT UNSIGNED NOT NULL,
    order_no VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL,
    paid_at DATETIME(6) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_orders_order_no (order_no),
    KEY idx_orders_status_paid_at_id (status, paid_at, id),
    CONSTRAINT chk_orders_status CHECK (status IN ('paid', 'unpaid', 'cancelled')),
    CONSTRAINT chk_orders_paid_at CHECK (status <> 'paid' OR paid_at IS NOT NULL)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='order-query-agent simulated data v1';

CREATE TABLE IF NOT EXISTS order_items (
    id BIGINT UNSIGNED NOT NULL,
    order_id BIGINT UNSIGNED NOT NULL,
    product_id BIGINT UNSIGNED NOT NULL,
    quantity INT NOT NULL,
    unit_price DECIMAL(12,2) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_items_order (order_id),
    KEY idx_items_product (product_id),
    CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES orders(id),
    CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES products(id),
    CONSTRAINT chk_items_quantity CHECK (quantity > 0),
    CONSTRAINT chk_items_unit_price CHECK (unit_price >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='order-query-agent simulated data v1';
