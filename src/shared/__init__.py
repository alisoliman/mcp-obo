"""What both workloads must agree on, and nothing else.

Anything here ships in both images, so it stays free of framework imports: the
chat app must not need fastmcp to know a tool's name, and the server must not
need Starlette to know which mode it is running.
"""
