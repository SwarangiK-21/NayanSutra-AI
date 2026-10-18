import os
import sys
import uuid

if sys.platform == "win32":
    # Ensure UTF-8 output on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from graph import _text, build_graph


def main():
    print("\n" + "=" * 60)
    print(" ⚖️  NyaySutra AI — Agentic Legal RAG Assistant")
    print("=" * 60)
    print("Commands: type 'exit' to quit | 'clear' to start new thread\n")

    graph = build_graph()
    thread_id = str(uuid.uuid4())
    cfg = {"configurable": {"thread_id": thread_id}}

    while True:
        try:
            q = input("\n⚖️ you> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting NyaySutra AI.")
            sys.exit(0)

        if not q:
            continue
        if q.lower() in {"exit", "quit"}:
            print("Session terminated.")
            sys.exit(0)
        if q.lower() == "clear":
            thread_id = str(uuid.uuid4())
            cfg = {"configurable": {"thread_id": thread_id}}
            print(f"Started new thread: {thread_id}")
            continue

        print("\n[Executing workflow graph...]")
        final = None
        trace = []
        try:
            for update in graph.stream({"messages": [("user", q)]}, cfg, stream_mode="updates"):
                for node, delta in update.items():
                    trace.append(node)
                    print(f"  └─► [{node}]")
                    if delta and delta.get("messages"):
                        final = delta["messages"][-1]

            print("\n" + "-" * 60)
            if final:
                print(_text(final))
            else:
                print("(No response generated)")
            print("-" * 60)
        except Exception as err:
            print(f"\n[Error during execution]: {err}")


if __name__ == "__main__":
    main()
