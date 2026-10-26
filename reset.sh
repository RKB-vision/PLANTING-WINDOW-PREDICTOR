#!/usr/bin/env bash
# reset.sh — Reset the database to a clean state (backs up first)
set -e

DB_FILE="plant_readiness.db"
BACKUP_DIR="data/generated/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

# Back up database if it exists
if [ -f "$DB_FILE" ]; then
  cp "$DB_FILE" "$BACKUP_DIR/${DB_FILE}.${TIMESTAMP}.bak"
  echo "Backed up $DB_FILE → $BACKUP_DIR/${DB_FILE}.${TIMESTAMP}.bak"
  rm -f "$DB_FILE"
  echo "Removed $DB_FILE"
fi

# Back up processed Cloudinary IDs
if [ -f "data/generated/processed_cloudinary_ids.txt" ]; then
  cp "data/generated/processed_cloudinary_ids.txt" "$BACKUP_DIR/processed_cloudinary_ids.${TIMESTAMP}.txt"
  rm -f "data/generated/processed_cloudinary_ids.txt"
  echo "Cleared processed_cloudinary_ids.txt"
fi

# Re-seed from CSV if requested
if [ "$1" = "--seed" ]; then
  echo "Seeding database from data/super_orange_growth_data.csv..."
  python cli.py seed --csv data/super_orange_growth_data.csv --species "Sunflower (SuperOrange)"
  echo "Database seeded."
else
  echo "Database reset. Run 'python cli.py seed --csv data/super_orange_growth_data.csv' to seed."
fi

echo "Reset complete."