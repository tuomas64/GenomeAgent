#!/usr/bin/env python3

import sqlite3
from pathlib import Path
from datetime import datetime


class Memory:
    def __init__(self, db_path="data/genomeagent_memory.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()

    def create_tables(self):
        cur = self.conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS directories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            path TEXT UNIQUE NOT NULL,
            cluster TEXT,
            category TEXT,
            description TEXT,
            scientific_value TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS software (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            version TEXT,
            location TEXT,
            module TEXT,
            container TEXT,
            cluster TEXT,
            purpose TEXT,
            validated INTEGER DEFAULT 0,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS protocols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            domain TEXT,
            purpose TEXT,
            status TEXT,
            source TEXT,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_name TEXT NOT NULL,
            relation TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_name TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            evidence TEXT,
            created_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            lesson TEXT NOT NULL,
            evidence TEXT,
            confidence REAL DEFAULT 1.0,
            created_at TEXT
        )
        """)

        self.conn.commit()

    def now(self):
        return datetime.now().isoformat(timespec="seconds")

    def add_project(self, name, description=""):
        cur = self.conn.cursor()
        now = self.now()
        cur.execute("""
        INSERT INTO projects (name, description, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            description=excluded.description,
            updated_at=excluded.updated_at
        """, (name, description, now, now))
        self.conn.commit()

    def add_directory(self, name, path, cluster="", category="", description="", scientific_value=""):
        cur = self.conn.cursor()
        now = self.now()
        cur.execute("""
        INSERT INTO directories
        (name, path, cluster, category, description, scientific_value, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            name=excluded.name,
            cluster=excluded.cluster,
            category=excluded.category,
            description=excluded.description,
            scientific_value=excluded.scientific_value,
            updated_at=excluded.updated_at
        """, (name, path, cluster, category, description, scientific_value, now, now))
        self.conn.commit()

    def add_software(self, name, version="", location="", module="", container="", cluster="", purpose="", validated=False, notes=""):
        cur = self.conn.cursor()
        now = self.now()
        cur.execute("""
        INSERT INTO software
        (name, version, location, module, container, cluster, purpose, validated, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, version, location, module, container, cluster, purpose, int(validated), notes, now, now))
        self.conn.commit()

    def add_protocol(self, name, domain="", purpose="", status="draft", source="", notes=""):
        cur = self.conn.cursor()
        now = self.now()
        cur.execute("""
        INSERT INTO protocols
        (name, domain, purpose, status, source, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            domain=excluded.domain,
            purpose=excluded.purpose,
            status=excluded.status,
            source=excluded.source,
            notes=excluded.notes,
            updated_at=excluded.updated_at
        """, (name, domain, purpose, status, source, notes, now, now))
        self.conn.commit()

    def add_relationship(self, source_type, source_name, relation, target_type, target_name, confidence=1.0, evidence=""):
        cur = self.conn.cursor()
        cur.execute("""
        INSERT INTO relationships
        (source_type, source_name, relation, target_type, target_name, confidence, evidence, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (source_type, source_name, relation, target_type, target_name, confidence, evidence, self.now()))
        self.conn.commit()

    def add_lesson(self, topic, lesson, evidence="", confidence=1.0):
        cur = self.conn.cursor()
        cur.execute("""
        INSERT INTO lessons
        (topic, lesson, evidence, confidence, created_at)
        VALUES (?, ?, ?, ?, ?)
        """, (topic, lesson, evidence, confidence, self.now()))
        self.conn.commit()

    def list_table(self, table):
        allowed = {"projects", "directories", "software", "protocols", "relationships", "lessons"}
        if table not in allowed:
            raise ValueError(f"Unknown table: {table}")
        cur = self.conn.cursor()
        cur.execute(f"SELECT * FROM {table} ORDER BY id")
        return cur.fetchall()

    def summary(self):
        cur = self.conn.cursor()
        tables = ["projects", "directories", "software", "protocols", "relationships", "lessons"]
        out = {}
        for t in tables:
            cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
            out[t] = cur.fetchone()["n"]
        return out
