-- Preserve censored observations separately from completed measurements.
ALTER TABLE pqo.sequential_analysis
    ADD COLUMN IF NOT EXISTS baseline_timed_out boolean NOT NULL DEFAULT false;
