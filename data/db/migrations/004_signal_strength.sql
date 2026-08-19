ALTER TABLE materials ADD COLUMN signal_score INTEGER;
ALTER TABLE materials ADD COLUMN signal_strength TEXT NOT NULL DEFAULT 'strong';

CREATE INDEX IF NOT EXISTS idx_materials_signal_strength ON materials(signal_strength);
