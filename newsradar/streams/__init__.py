"""Stream consumers: long-running processes that read a public firehose and
store counts, never content (R30, R31). Each resumes from a cursor stored in
`stream_cursor`, committed in the same transaction as the rows it covers."""
