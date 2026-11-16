-- Dedicated synthetic source/reference for INSERT SELECT. Never overwrite labs.
CREATE SCHEMA retail;
CREATE TABLE retail.categories(group_id integer PRIMARY KEY,status text NOT NULL);
INSERT INTO retail.categories VALUES(1,'active'),(2,'reserved'),(3,'active'),(4,'active'),(5,'reserved');
CREATE TABLE retail.incoming (
    incoming_id integer PRIMARY KEY,
    sku text NOT NULL,
    warehouse_id integer NOT NULL,
    quantity integer NOT NULL,
    group_id integer NOT NULL
);
CREATE TABLE retail.inventory (
    stock_id bigserial PRIMARY KEY,
    sku text NOT NULL,
    warehouse_id integer NOT NULL,
    quantity integer NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'active',
    UNIQUE(sku,warehouse_id)
);
INSERT INTO retail.incoming
SELECT i,'item-'||i,i%10+1,i%100,i%5+1 FROM generate_series(1,50000) AS i;
INSERT INTO retail.inventory(sku,warehouse_id,quantity) VALUES('item-7',8,10);
ANALYZE retail.incoming;
ANALYZE retail.categories;
ANALYZE retail.inventory;
