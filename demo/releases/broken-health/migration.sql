ALTER TABLE notes
ADD COLUMN category TEXT NOT NULL DEFAULT 'general'
CHECK(length(trim(category)) > 0);
PRAGMA user_version = 2;
