CREATE TABLE notes (
    id INTEGER PRIMARY KEY,
    body TEXT NOT NULL CHECK(length(trim(body)) > 0)
);
PRAGMA user_version = 1;
