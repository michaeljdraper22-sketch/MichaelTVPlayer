"""Lightweight data containers used across the app."""

from dataclasses import dataclass


@dataclass
class UserInfo:
    username: str = ""
    status: str = ""
    exp_date: str = ""
    is_trial: str = ""
    active_cons: str = ""
    max_connections: str = ""
    created_at: str = ""


@dataclass
class EpgEntry:
    title: str = ""
    description: str = ""
    start: str = ""
    end: str = ""
    start_timestamp: str = ""
    stop_timestamp: str = ""
