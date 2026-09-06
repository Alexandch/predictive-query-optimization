-- CH-benCHmark-compatible order-entry schema.
-- The structure follows the Apache-2.0 BenchBase PostgreSQL definitions:
-- https://github.com/cmu-db/benchbase/tree/main/src/main/resources/benchmarks

CREATE SCHEMA IF NOT EXISTS chbenchmark;

CREATE TABLE IF NOT EXISTS chbenchmark.region (
    r_regionkey integer PRIMARY KEY,
    r_name varchar(25) NOT NULL UNIQUE,
    r_comment varchar(152) NOT NULL
);

CREATE TABLE IF NOT EXISTS chbenchmark.nation (
    n_nationkey integer PRIMARY KEY,
    n_name varchar(25) NOT NULL UNIQUE,
    n_regionkey integer NOT NULL REFERENCES chbenchmark.region (r_regionkey),
    n_comment varchar(152) NOT NULL
);

CREATE TABLE IF NOT EXISTS chbenchmark.supplier (
    su_suppkey integer PRIMARY KEY,
    su_name varchar(25) NOT NULL,
    su_address varchar(40) NOT NULL,
    su_nationkey integer NOT NULL REFERENCES chbenchmark.nation (n_nationkey),
    su_phone varchar(15) NOT NULL,
    su_acctbal numeric(12, 2) NOT NULL,
    su_comment varchar(101) NOT NULL
);

CREATE TABLE IF NOT EXISTS chbenchmark.warehouse (
    w_id integer PRIMARY KEY,
    w_ytd numeric(12, 2) NOT NULL,
    w_tax numeric(4, 4) NOT NULL,
    w_name varchar(20) NOT NULL,
    w_city varchar(30) NOT NULL,
    w_state char(2) NOT NULL,
    w_zip char(9) NOT NULL
);

CREATE TABLE IF NOT EXISTS chbenchmark.district (
    d_w_id integer NOT NULL REFERENCES chbenchmark.warehouse (w_id),
    d_id integer NOT NULL,
    d_ytd numeric(12, 2) NOT NULL,
    d_tax numeric(4, 4) NOT NULL,
    d_next_o_id integer NOT NULL,
    d_name varchar(20) NOT NULL,
    d_city varchar(30) NOT NULL,
    d_state char(2) NOT NULL,
    d_zip char(9) NOT NULL,
    PRIMARY KEY (d_w_id, d_id)
);

CREATE TABLE IF NOT EXISTS chbenchmark.customer (
    c_w_id integer NOT NULL,
    c_d_id integer NOT NULL,
    c_id integer NOT NULL,
    c_discount numeric(4, 4) NOT NULL,
    c_credit char(2) NOT NULL,
    c_last varchar(20) NOT NULL,
    c_first varchar(20) NOT NULL,
    c_credit_lim numeric(12, 2) NOT NULL,
    c_balance numeric(12, 2) NOT NULL,
    c_ytd_payment numeric(12, 2) NOT NULL,
    c_payment_cnt integer NOT NULL,
    c_delivery_cnt integer NOT NULL,
    c_city varchar(30) NOT NULL,
    c_state char(2) NOT NULL,
    c_zip char(9) NOT NULL,
    c_phone char(16) NOT NULL,
    c_since timestamp NOT NULL,
    c_data varchar(120) NOT NULL,
    FOREIGN KEY (c_w_id, c_d_id)
        REFERENCES chbenchmark.district (d_w_id, d_id),
    PRIMARY KEY (c_w_id, c_d_id, c_id)
);

CREATE TABLE IF NOT EXISTS chbenchmark.item (
    i_id integer PRIMARY KEY,
    i_name varchar(32) NOT NULL,
    i_price numeric(8, 2) NOT NULL,
    i_data varchar(64) NOT NULL,
    i_im_id integer NOT NULL
);

CREATE TABLE IF NOT EXISTS chbenchmark.stock (
    s_w_id integer NOT NULL REFERENCES chbenchmark.warehouse (w_id),
    s_i_id integer NOT NULL REFERENCES chbenchmark.item (i_id),
    s_quantity integer NOT NULL,
    s_ytd numeric(12, 2) NOT NULL,
    s_order_cnt integer NOT NULL,
    s_remote_cnt integer NOT NULL,
    s_data varchar(64) NOT NULL,
    PRIMARY KEY (s_w_id, s_i_id)
);

CREATE TABLE IF NOT EXISTS chbenchmark.oorder (
    o_w_id integer NOT NULL,
    o_d_id integer NOT NULL,
    o_id integer NOT NULL,
    o_c_id integer NOT NULL,
    o_carrier_id integer,
    o_ol_cnt integer NOT NULL,
    o_all_local boolean NOT NULL,
    o_entry_d timestamp NOT NULL,
    FOREIGN KEY (o_w_id, o_d_id, o_c_id)
        REFERENCES chbenchmark.customer (c_w_id, c_d_id, c_id),
    PRIMARY KEY (o_w_id, o_d_id, o_id)
);

CREATE TABLE IF NOT EXISTS chbenchmark.new_order (
    no_w_id integer NOT NULL,
    no_d_id integer NOT NULL,
    no_o_id integer NOT NULL,
    FOREIGN KEY (no_w_id, no_d_id, no_o_id)
        REFERENCES chbenchmark.oorder (o_w_id, o_d_id, o_id),
    PRIMARY KEY (no_w_id, no_d_id, no_o_id)
);

CREATE TABLE IF NOT EXISTS chbenchmark.order_line (
    ol_w_id integer NOT NULL,
    ol_d_id integer NOT NULL,
    ol_o_id integer NOT NULL,
    ol_number integer NOT NULL,
    ol_i_id integer NOT NULL,
    ol_delivery_d timestamp,
    ol_amount numeric(10, 2) NOT NULL,
    ol_supply_w_id integer NOT NULL,
    ol_quantity numeric(8, 2) NOT NULL,
    ol_dist_info varchar(24) NOT NULL,
    FOREIGN KEY (ol_w_id, ol_d_id, ol_o_id)
        REFERENCES chbenchmark.oorder (o_w_id, o_d_id, o_id),
    FOREIGN KEY (ol_supply_w_id, ol_i_id)
        REFERENCES chbenchmark.stock (s_w_id, s_i_id),
    PRIMARY KEY (ol_w_id, ol_d_id, ol_o_id, ol_number)
);

CREATE TABLE IF NOT EXISTS chbenchmark.history (
    h_id bigint PRIMARY KEY,
    h_c_id integer NOT NULL,
    h_c_d_id integer NOT NULL,
    h_c_w_id integer NOT NULL,
    h_d_id integer NOT NULL,
    h_w_id integer NOT NULL,
    h_date timestamp NOT NULL,
    h_amount numeric(8, 2) NOT NULL,
    h_data varchar(32) NOT NULL,
    FOREIGN KEY (h_c_w_id, h_c_d_id, h_c_id)
        REFERENCES chbenchmark.customer (c_w_id, c_d_id, c_id),
    FOREIGN KEY (h_w_id, h_d_id)
        REFERENCES chbenchmark.district (d_w_id, d_id)
);
