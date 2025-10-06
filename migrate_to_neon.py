#!/usr/bin/env python3
"""
Migration script to transfer user memory data from SQLite to Neon PostgreSQL.
This script will:
1. Read all data from the local SQLite database
2. Connect to Neon PostgreSQL
3. Create the table if it doesn't exist
4. Transfer all records
5. Verify the migration
"""

import os
import sqlite3
import psycopg2
import json
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Database URLs
SQLITE_DB_PATH = 'user_memory.db'
NEON_DATABASE_URL = os.getenv('DATABASE_URL') or 'postgresql://neondb_owner:npg_jknV5xhGL0eR@ep-rapid-violet-agn8ppi2-pooler.c-2.eu-central-1.aws.neon.tech/neondb?sslmode=require'

def migrate_data():
    """Main migration function"""
    print("=" * 60)
    print("Starting migration from SQLite to Neon PostgreSQL")
    print("=" * 60)
    
    # Step 1: Read data from SQLite
    print("\n[1/5] Reading data from SQLite database...")
    sqlite_conn = sqlite3.connect(SQLITE_DB_PATH)
    sqlite_cursor = sqlite_conn.cursor()
    
    try:
        sqlite_cursor.execute("SELECT * FROM user_memory")
        rows = sqlite_cursor.fetchall()
        print(f"✓ Found {len(rows)} records in SQLite database")
        
        if len(rows) == 0:
            print("⚠ No data to migrate. Exiting.")
            return
        
        # Print column names for reference
        column_names = [description[0] for description in sqlite_cursor.description]
        print(f"✓ Columns: {', '.join(column_names)}")
        
    except sqlite3.OperationalError as e:
        print(f"✗ Error reading SQLite database: {e}")
        print("Make sure the user_memory.db file exists.")
        return
    finally:
        sqlite_conn.close()
    
    # Step 2: Connect to PostgreSQL
    print("\n[2/5] Connecting to Neon PostgreSQL...")
    try:
        pg_conn = psycopg2.connect(NEON_DATABASE_URL)
        pg_cursor = pg_conn.cursor()
        print("✓ Successfully connected to Neon PostgreSQL")
    except Exception as e:
        print(f"✗ Error connecting to PostgreSQL: {e}")
        return
    
    # Step 3: Create table if it doesn't exist
    print("\n[3/5] Creating table if it doesn't exist...")
    try:
        pg_cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_memory (
                user_id BIGINT PRIMARY KEY,
                name TEXT,
                age TEXT,
                interests TEXT,
                preferences TEXT,
                important_facts TEXT,
                pinned_messages TEXT
            )
        ''')
        pg_conn.commit()
        print("✓ Table created/verified successfully")
    except Exception as e:
        print(f"✗ Error creating table: {e}")
        pg_conn.close()
        return
    
    # Step 4: Transfer data
    print("\n[4/5] Transferring data...")
    migrated_count = 0
    error_count = 0
    
    for row in rows:
        user_id, name, age, interests, preferences, important_facts, pinned_messages = row
        
        try:
            # Use INSERT ... ON CONFLICT to handle duplicates
            pg_cursor.execute('''
                INSERT INTO user_memory (user_id, name, age, interests, preferences, important_facts, pinned_messages)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    age = EXCLUDED.age,
                    interests = EXCLUDED.interests,
                    preferences = EXCLUDED.preferences,
                    important_facts = EXCLUDED.important_facts,
                    pinned_messages = EXCLUDED.pinned_messages
            ''', (user_id, name, age, interests, preferences, important_facts, pinned_messages))
            
            migrated_count += 1
            print(f"  ✓ Migrated user_id: {user_id} ({name or 'No name'})")
            
        except Exception as e:
            error_count += 1
            print(f"  ✗ Error migrating user_id {user_id}: {e}")
    
    # Commit all changes
    pg_conn.commit()
    print(f"\n✓ Successfully migrated {migrated_count} records")
    if error_count > 0:
        print(f"⚠ {error_count} records failed to migrate")
    
    # Step 5: Verify migration
    print("\n[5/5] Verifying migration...")
    pg_cursor.execute("SELECT COUNT(*) FROM user_memory")
    pg_count = pg_cursor.fetchone()[0]
    print(f"✓ PostgreSQL now contains {pg_count} records")
    
    # Show a sample of migrated data
    print("\n" + "=" * 60)
    print("Sample of migrated data:")
    print("=" * 60)
    pg_cursor.execute("SELECT user_id, name, age, interests FROM user_memory LIMIT 3")
    sample_rows = pg_cursor.fetchall()
    for row in sample_rows:
        user_id, name, age, interests = row
        print(f"\nUser ID: {user_id}")
        print(f"  Name: {name or 'N/A'}")
        print(f"  Age: {age or 'N/A'}")
        print(f"  Interests: {interests or 'N/A'}")
    
    # Close connections
    pg_cursor.close()
    pg_conn.close()
    
    print("\n" + "=" * 60)
    print("Migration completed successfully! 🎉")
    print("=" * 60)
    print("\nYour bot is now using Neon PostgreSQL for both local and production.")
    print("The old SQLite database (user_memory.db) has been kept as a backup.")
    print("\nNext steps:")
    print("1. Test the bot to ensure everything works correctly")
    print("2. If everything works, you can keep user_memory.db as a backup")
    print("3. Set DATABASE_URL in your .env file (if not already set)")
    print("4. Deploy to production with the same DATABASE_URL")

if __name__ == "__main__":
    try:
        migrate_data()
    except KeyboardInterrupt:
        print("\n\n⚠ Migration cancelled by user")
    except Exception as e:
        print(f"\n\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()


