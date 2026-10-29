-- NULL means the historical analysis predates evidence recording, not zero trials.
ALTER TABLE pqo.sequential_analysis
    ADD COLUMN IF NOT EXISTS candidate_trial_evidence jsonb;
