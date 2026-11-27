import argparse
import json
from pathlib import Path

from .adapters import ModelProfile, OllamaStudentAdapter
from .experiment import FAST_DIGEST, run_first_experiment
from .mentors import MentorFeedback
from .school import School


def main(argv=None):
    parser = argparse.ArgumentParser(description="School v0.1: proposal/static-only laboratory")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--model", default="qwen7b-fast:latest")
    parser.add_argument("--expected-digest", default=FAST_DIGEST)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("experiment")
    commands.add_parser("catalog")
    export = commands.add_parser("export")
    export.add_argument("session_id")
    feedback = commands.add_parser("feedback")
    feedback.add_argument("run_id")
    feedback.add_argument("--role", choices=["gpt_architect", "gpt_engineer"], required=True)
    feedback.add_argument("--author", required=True)
    feedback.add_argument("--text", required=True)
    args = parser.parse_args(argv)
    try:
        profile = ModelProfile.build(args.model, args.expected_digest)
        if args.command == "experiment":
            result = run_first_experiment(args.state_dir, profile=profile)
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0 if result["status"] == "response_received_static_only" else 2
        with School(Path(args.state_dir), OllamaStudentAdapter(profile)) as school:
            if args.command == "catalog":
                print(json.dumps(school.catalog(), ensure_ascii=True, indent=2))
            elif args.command == "export":
                print(school.export_session(args.session_id))
            elif args.command == "feedback":
                key = school.add_feedback(args.run_id, MentorFeedback(args.role, args.author, args.text))
                print(json.dumps({"feedback_id": key, "origin": "manual"}))
        return 0
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
