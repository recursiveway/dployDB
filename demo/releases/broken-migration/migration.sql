CREATE TABLE provisional_notes (
    id INTEGER PRIMARY KEY,
    note_id INTEGER NOT NULL REFERENCES notes(id)
);
INSERT INTO provisional_notes (id, note_id)
SELECT id, id FROM deliberate_missing_table;
PRAGMA user_version = 2;
