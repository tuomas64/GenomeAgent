#!/usr/bin/env python3

import argparse
from genomeagent.memory import Memory
from genomeagent.seed_memory import seed


def print_rows(rows):
    if not rows:
        print("No records.")
        return
    for row in rows:
        print(dict(row))


def main():
    parser = argparse.ArgumentParser(description="GenomeAgent command line interface")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init-memory", help="Create and seed GenomeAgent memory database")
    sub.add_parser("summary", help="Show memory summary")

    list_parser = sub.add_parser("list", help="List a memory table")
    list_parser.add_argument("table", choices=["projects", "directories", "software", "protocols", "relationships", "lessons"])

    lesson_parser = sub.add_parser("lesson", help="Add a lesson learned")
    lesson_parser.add_argument("topic")
    lesson_parser.add_argument("lesson")

    args = parser.parse_args()

    if args.command == "init-memory":
        seed()
    elif args.command == "summary":
        db = Memory()
        for key, value in db.summary().items():
            print(f"{key}: {value}")
    elif args.command == "list":
        db = Memory()
        print_rows(db.list_table(args.table))
    elif args.command == "lesson":
        db = Memory()
        db.add_lesson(args.topic, args.lesson)
        print("Lesson added.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
