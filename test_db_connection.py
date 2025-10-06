#!/usr/bin/env python3
"""
Quick test to verify the PostgreSQL database connection is working correctly.
"""

import asyncio
import os
from dotenv import load_dotenv
import psycopg2

# Load environment variables
load_dotenv()

DATABASE_URL = os.getenv('DATABASE_URL') or 'postgresql://neondb_owner:npg_jknV5xhGL0eR@ep-rapid-violet-agn8ppi2-pooler.c-2.eu-central-1.aws.neon.tech/neondb?sslmode=require'

def test_connection():
    """Test basic database connection and queries"""
    print("Testing Neon PostgreSQL connection...")
    print("=" * 60)
    
    try:
        # Test connection
        print("\n[1] Connecting to database...")
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        print("✓ Connected successfully!")
        
        # Test query
        print("\n[2] Testing SELECT query...")
        cursor.execute("SELECT COUNT(*) FROM user_memory")
        count = cursor.fetchone()[0]
        print(f"✓ Query successful! Found {count} user(s) in database")
        
        # Test retrieving data
        print("\n[3] Retrieving sample data...")
        cursor.execute("SELECT user_id, name, age FROM user_memory LIMIT 3")
        rows = cursor.fetchall()
        
        if rows:
            print("✓ Sample data retrieved:")
            for user_id, name, age in rows:
                print(f"   - User {user_id}: {name or 'N/A'} (Age: {age or 'N/A'})")
        else:
            print("⚠ No data found in database")
        
        # Test INSERT/UPDATE (with rollback to avoid modifying data)
        print("\n[4] Testing INSERT capability (will be rolled back)...")
        cursor.execute("""
            INSERT INTO user_memory (user_id, name, age)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
            RETURNING user_id
        """, (999999999, 'Test User', '99'))
        
        result = cursor.fetchone()
        if result:
            print(f"✓ INSERT test successful (test user_id: {result[0]})")
        else:
            print("✓ INSERT test successful (user already exists, no conflict)")
        
        # Rollback to avoid modifying data
        conn.rollback()
        print("✓ Test transaction rolled back (no actual data modified)")
        
        # Close connection
        cursor.close()
        conn.close()
        
        print("\n" + "=" * 60)
        print("✅ All tests passed! Database is working correctly.")
        print("=" * 60)
        print("\nYou can now run your bot with confidence!")
        print("Both local and production will use the same Neon database.")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("\nPlease check:")
        print("1. Your DATABASE_URL is correct")
        print("2. You have internet connection")
        print("3. The Neon database is accessible")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_connection()


