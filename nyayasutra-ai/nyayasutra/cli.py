import sys
import uuid

from graph import _text, build_graph


def main():
    graph = build_graph()
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    print("Nyayasutra AI — type 'exit' to quit\n")
    while True:
        q = input("you> ").strip()
        if q.lower() in {"exit", "quit"}:
            sys.exit(0)
        final = None
        for update in graph.stream({"messages": [("user", q)]}, cfg, stream_mode="updates"):
            for node, delta in update.items():
                print(f"  · {node}")
                if delta and delta.get("messages"):
                    final = delta["messages"][-1]
        print(f"\n{_text(final)}\n" if final else "\n(no answer)\n")


if __name__ == "__main__":
    main()
