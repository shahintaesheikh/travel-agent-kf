-- Extensions are created by the first migration, but having them here ensures
-- the Docker image has them available. The migration uses CREATE EXTENSION IF NOT EXISTS.
-- This file is mounted into the Docker entrypoint.
SELECT 1;