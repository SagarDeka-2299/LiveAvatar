#!/usr/bin/env python3
"""Simli Face Cleanup Utility.

Lists all generated GS faces on your Simli account and deletes them to free up
your subscription quota. Useful if you cleared your database but still have
stale faces lingering on Simli.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dotenv import load_dotenv

# Ensure we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.simli_client import list_faces, delete_face


async def main() -> None:
    load_dotenv()
    
    api_key = os.getenv("SIMLI_API_KEY", "").strip()
    if not api_key:
        print("❌ Error: SIMLI_API_KEY is not defined in .env", file=sys.stderr)
        sys.exit(1)
        
    print(f"🔍 Fetching faces from Simli API (Key: {api_key[:6]}...{api_key[-4:] if len(api_key) > 10 else ''})...")
    
    try:
        faces = await list_faces(api_key)
    except Exception as exc:
        print(f"❌ Error listing faces from Simli: {exc}", file=sys.stderr)
        sys.exit(1)
        
    if not faces:
        print("✅ No faces found on your Simli account. Quota is fully clean!")
        return

    print(f"📋 Found {len(faces)} faces:")
    for idx, face in enumerate(faces, 1):
        face_id = face.get("face_id") or face.get("id") or "unknown"
        face_name = face.get("face_name") or face.get("name") or "Unnamed"
        created_at = face.get("createdAt") or face.get("created_at") or "unknown date"
        print(f"  {idx}. ID: {face_id} | Name: '{face_name}' | Created: {created_at}")

    print("\n⚠️ WARNING: Deleting a face will break any active assistants relying on it.")
    choice = input("Do you want to delete ALL these faces from Simli? (yes/no): ").strip().lower()
    
    if choice != "yes":
        print("❌ Cleanup cancelled.")
        return

    print("\n🧹 Starting cleanup of faces...")
    success_count = 0
    failure_count = 0
    
    for face in faces:
        face_id = face.get("face_id") or face.get("id")
        face_name = face.get("face_name") or face.get("name") or "Unnamed"
        if not face_id:
            continue
            
        print(f"🗑️ Deleting face '{face_name}' ({face_id})...")
        try:
            await delete_face(api_key, face_id)
            print("  ✅ Deleted successfully.")
            success_count += 1
        except Exception as exc:
            print(f"  ⚠️ Failed to delete: {exc}", file=sys.stderr)
            failure_count += 1
            
    print(f"\n🎉 Cleanup complete! Successfully deleted {success_count} faces. {failure_count} failures.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")
