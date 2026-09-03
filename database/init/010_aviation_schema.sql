CREATE SCHEMA IF NOT EXISTS aviation;

CREATE TABLE IF NOT EXISTS aviation.airports (
    airport_code char(3) PRIMARY KEY,
    airport_name text NOT NULL,
    city text NOT NULL,
    timezone text NOT NULL
);

CREATE TABLE IF NOT EXISTS aviation.aircrafts (
    aircraft_code char(3) PRIMARY KEY,
    model text NOT NULL,
    range_km integer NOT NULL CHECK (range_km > 0)
);

CREATE TABLE IF NOT EXISTS aviation.seats (
    aircraft_code char(3) NOT NULL
        REFERENCES aviation.aircrafts (aircraft_code),
    seat_no varchar(4) NOT NULL,
    fare_conditions varchar(16) NOT NULL
        CHECK (fare_conditions IN ('Economy', 'Comfort', 'Business')),
    PRIMARY KEY (aircraft_code, seat_no)
);

CREATE TABLE IF NOT EXISTS aviation.flights (
    flight_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    flight_no varchar(6) NOT NULL,
    scheduled_departure timestamptz NOT NULL,
    scheduled_arrival timestamptz NOT NULL,
    departure_airport char(3) NOT NULL
        REFERENCES aviation.airports (airport_code),
    arrival_airport char(3) NOT NULL
        REFERENCES aviation.airports (airport_code),
    status varchar(20) NOT NULL,
    aircraft_code char(3) NOT NULL
        REFERENCES aviation.aircrafts (aircraft_code),
    CHECK (arrival_airport <> departure_airport),
    CHECK (scheduled_arrival > scheduled_departure)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_flights_number_departure
    ON aviation.flights (flight_no, scheduled_departure);

CREATE TABLE IF NOT EXISTS aviation.bookings (
    book_ref char(6) PRIMARY KEY,
    book_date timestamptz NOT NULL,
    total_amount numeric(12, 2) NOT NULL CHECK (total_amount >= 0)
);

CREATE TABLE IF NOT EXISTS aviation.tickets (
    ticket_no char(13) PRIMARY KEY,
    book_ref char(6) NOT NULL REFERENCES aviation.bookings (book_ref),
    passenger_id varchar(20) NOT NULL,
    passenger_name text NOT NULL
);

CREATE TABLE IF NOT EXISTS aviation.ticket_flights (
    ticket_no char(13) NOT NULL REFERENCES aviation.tickets (ticket_no),
    flight_id integer NOT NULL REFERENCES aviation.flights (flight_id),
    fare_conditions varchar(16) NOT NULL
        CHECK (fare_conditions IN ('Economy', 'Comfort', 'Business')),
    amount numeric(12, 2) NOT NULL CHECK (amount >= 0),
    PRIMARY KEY (ticket_no, flight_id)
);

CREATE TABLE IF NOT EXISTS aviation.boarding_passes (
    ticket_no char(13) NOT NULL,
    flight_id integer NOT NULL,
    boarding_no integer NOT NULL CHECK (boarding_no > 0),
    seat_no varchar(4) NOT NULL,
    PRIMARY KEY (ticket_no, flight_id),
    UNIQUE (flight_id, boarding_no),
    UNIQUE (flight_id, seat_no),
    FOREIGN KEY (ticket_no, flight_id)
        REFERENCES aviation.ticket_flights (ticket_no, flight_id)
);
