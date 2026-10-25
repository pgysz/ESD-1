"""Shared business logic for the attendance system.

Both transports -- the Flask HTTP API (app.py) and the gRPC service
(grpc_service/server.py) -- import this package. That is how they share the
same business rules AND the same source of truth (one SQLite database file),
which is what Assignment 2 Part B requires. Neither transport contains any
attendance rules of its own; they only translate their wire format to calls
on AttendanceService and translate its results/errors back.
"""
