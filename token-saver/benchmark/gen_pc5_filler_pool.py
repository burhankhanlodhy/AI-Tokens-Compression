#!/usr/bin/env python3
"""Generate pc5_filler_pool.txt: 600 distinct, realistic, traffic-shaped prompts.

Role: filler volume for the calibration harness. Every line is a real support /
self-serve / API / dev-ops question a user could type. Pool vectors must NOT
duplicate any corpus stored vector; distinctness inside the pool is asserted.
"""
TOPICS = [
    ("reset", ["change my display name", "update my profile bio", "remove my profile photo",
               "rename my workspace", "change my username", "edit my account details"]),
    ("security", ["turn on login alerts", "review active sessions", "sign out all devices",
                  "set up a recovery email", "enable login approval requests", "view my security history"]),
    ("billing", ["apply a promo code", "redeem a gift subscription", "view my payment history",
                 "change my billing cycle", "get a receipt for my payment", "dispute a charge on my account"]),
    ("plans", ["switch to the free plan", "see what each plan includes", "compare plan features",
               "change my plan at renewal", "find my current plan details", "add more seats to my plan"]),
    ("files", ["upload a large file", "organize files into folders", "search inside my files",
               "preview a file without downloading", "restore an earlier version of a file",
               "move files between folders"]),
    ("team", ["see everyone in my organization", "set a member's role", "remove guest access",
              "create a new team channel", "archive an old project", "reassign a project owner"]),
    ("notifications", ["mute a channel", "set quiet hours", "get email digests instead of push",
                       "turn off marketing emails", "customize notification keywords", "stop notifications at night"]),
    ("api", ["test an endpoint before going live", "see my remaining quota", "handle 429 responses",
             "version my API integration", "get request logs for debugging", "retry a failed request safely"]),
    ("webhooks", ["list my registered endpoints", "test a webhook locally", "see webhook delivery logs",
                  "rotate a webhook signing secret", "subscribe to a new event type", "delete a webhook endpoint"]),
    ("keys", ["store API keys safely", "scope a key to read-only", "see when a key was last used",
              "set an expiry on a key", "label my keys for tracking", "restrict a key by IP range"]),
    ("data", ["schedule a recurring export", "get data in JSON instead of CSV", "export only recent records",
              "include attachments in an export", "export another user's data as an admin", "check export progress"]),
    ("workspace", ["create a second workspace", "merge two workspaces", "see workspace storage usage",
                   "transfer files between workspaces", "duplicate a workspace", "change workspace visibility"]),
    ("account", ["recover a hacked account", "merge two accounts", "change my language settings",
                 "see my login history", "close a duplicate account", "update my timezone"]),
    ("orders", ["cancel an order before shipping", "return part of an order", "get a delivery estimate",
                "find my order number", "contact the seller of an order", "report a damaged delivery"]),
    ("search", ["search by exact phrase", "filter search by date", "search only my starred items",
                "save a search for later", "exclude archived items from search", "search inside comments"]),
    ("integration", ["connect my calendar", "link a Slack workspace", "set up an email integration",
                     "sync contacts from my CRM", "connect a third-party storage provider", "remove an integration"]),
    ("mobile", ["use the app offline", "enable fingerprint unlock", "reduce mobile data usage",
                "scan a document with my phone", "share from the mobile app", "fix mobile app crashes"]),
    ("performance", ["speed up a slow dashboard", "reduce page load time", "check service status",
                     "see upcoming maintenance windows", "fix frequent timeouts", "improve sync performance"]),
    ("compliance", ["request a DPA", "see where data is stored", "get a security whitepaper",
                    "check SOC 2 status", "export audit trails for compliance", "configure data residency"]),
    ("developer", ["read a stack trace from the logs", "set up local development", "run the test suite",
                   "check out a pull request", "profile a slow function", "debug a failing build"]),
    ("databases", ["back up a database", "restore from a snapshot", "tune connection pool size",
                   "migrate a schema safely", "check replica lag", "compact a bloated table"]),
    ("cloud", ["resize an instance", "attach a persistent disk", "open a firewall port",
               "set up a load balancer", "create a DNS record", "rotate cloud credentials"]),
    ("containers", ["inspect a running container", "view container logs", "exec into a container",
                    "limit container memory", "rebuild an image", "prune unused volumes"]),
    ("emails", ["stop spam emails", "create a filter for newsletters", "recover a deleted email",
                "set up an auto-reply", "add a signature", "unblock a sender"]),
]
FORMS = [
    "How do I {t}?",
    "Can you show me how to {t}?",
    "What is the steps to {t}?",  # intentionally informal; real users type this
    "I need help to {t}.",
    "Where is the option to {t}?",
    "Is there a way to {t}?",
    "How can my team {t}?",
    "Please explain how to {t}.",
    "Steps for how to {t}?",
    "Hi, how do I {t}?",
    "How would I go about trying to {t}?",
    "Any instructions on how to {t}?",
]
out = []
seen = set()
for _, variants in TOPICS:
    for v in variants:
        for f in FORMS:
            s = f.format(t=v)
            if s not in seen:
                seen.add(s)
                out.append(s)
assert len(out) >= 600, len(out)
out = out[:600]
open("/tmp/pc5_filler_pool.txt", "w").write("\n".join(out) + "\n")
print("pool lines:", len(out))
