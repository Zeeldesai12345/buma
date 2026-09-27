import React, { useState, useEffect, useRef } from 'react';
import {
  Box,
  Typography,
  Card,
  CardContent,
  TextField,
  IconButton,
  Button,
  Chip,
  Alert,
  FormControl,
  InputLabel,
  Select,
  MenuItem,
  Link,
  CircularProgress,
} from '@mui/material';
import SendIcon from '@mui/icons-material/Send';
import StopIcon from '@mui/icons-material/Stop';
import AddCommentIcon from '@mui/icons-material/AddComment';
import SmartToyIcon from '@mui/icons-material/SmartToy';
import { chatApi, configApi } from '../services/api';

// "Ask Buma" (DD-27): questions about one repo's live triage data, answered by an agent that
// searches issues (semantic RAG over issue embeddings) and reads Buma's decisions and workload.

const TOOL_LABELS = {
  search_issues: 'Searching issues',
  get_issue: 'Reading issue',
  get_recent_triage: 'Checking recent triage',
  get_workload: 'Checking team workload',
  get_productivity: 'Checking productivity',
};

const SUGGESTIONS = [
  'Who on the team has spare capacity right now?',
  'Are there any open issues about login or authentication?',
  'Summarise the last 10 triage decisions.',
  'Who resolved the most issues in the last 30 days?',
];

// Must stay within the gateway's limits (routes/chat.py).
const MAX_HISTORY_TURNS = 20;
const MAX_TURN_CHARS = 8000;
const MAX_QUESTION_CHARS = 2000;

function renderWithIssueLinks(text, repoFullName) {
  if (!repoFullName) return text;
  return text.split(/(#\d+)/g).map((part, i) => {
    const match = /^#(\d+)$/.exec(part);
    if (!match) return part;
    return (
      <Link
        key={i}
        href={`https://github.com/${repoFullName}/issues/${match[1]}`}
        target="_blank"
        rel="noopener noreferrer"
      >
        {part}
      </Link>
    );
  });
}

function buildHistory(messages) {
  return messages
    .filter((m) => m.content.trim() && !m.streaming)
    .map((m) => ({ role: m.role, content: m.content.slice(0, MAX_TURN_CHARS) }))
    .slice(-MAX_HISTORY_TURNS);
}

export default function Assistant() {
  const [repositories, setRepositories] = useState([]);
  const [selectedRepoId, setSelectedRepoId] = useState(parseInt(localStorage.getItem('repo_id')) || null);
  const [status, setStatus] = useState(null); // {enabled, model} | null while loading
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [pageError, setPageError] = useState('');
  const abortRef = useRef(null);
  const bottomRef = useRef(null);

  useEffect(() => {
    configApi
      .getAllRepos({ limit: 100, offset: 0 })
      .then((response) => {
        const repos = response.data.repos || [];
        setRepositories(repos);
        if (repos.length > 0 && !repos.some((r) => r.repo_id === selectedRepoId)) {
          setSelectedRepoId(repos[0].repo_id);
        }
      })
      .catch(() => setPageError('Failed to load repositories'));

    chatApi
      .getStatus()
      .then((response) => setStatus(response.data))
      .catch(() => setStatus({ enabled: false, model: null }));

    return () => abortRef.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const currentRepo = repositories.find((r) => r.repo_id === selectedRepoId);

  const updateLast = (fn) =>
    setMessages((prev) => {
      const next = [...prev];
      next[next.length - 1] = fn(next[next.length - 1]);
      return next;
    });

  const handleEvent = (event) => {
    switch (event.type) {
      case 'text':
        updateLast((m) => ({ ...m, content: m.content + event.text, activeTool: null }));
        break;
      case 'tool':
        updateLast((m) => ({ ...m, activeTool: event.name, toolsUsed: [...m.toolsUsed, event.name] }));
        break;
      case 'sources':
        updateLast((m) => ({ ...m, sources: event.issues }));
        break;
      case 'error':
        updateLast((m) => ({ ...m, error: event.message }));
        break;
      case 'done':
        updateLast((m) => ({ ...m, streaming: false, activeTool: null }));
        break;
      default:
        break;
    }
  };

  const ask = async (question) => {
    const text = question.trim().slice(0, MAX_QUESTION_CHARS);
    if (!text || busy || !selectedRepoId) return;

    const history = buildHistory(messages);
    setMessages((prev) => [
      ...prev,
      { role: 'user', content: text, toolsUsed: [], sources: [] },
      { role: 'assistant', content: '', toolsUsed: [], sources: [], streaming: true, activeTool: null, error: '' },
    ]);
    setInput('');
    setBusy(true);

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await chatApi.ask(selectedRepoId, text, history, handleEvent, controller.signal);
    } catch (err) {
      if (err.name !== 'AbortError') {
        updateLast((m) => ({ ...m, error: err.message || 'Request failed' }));
      }
    } finally {
      updateLast((m) => ({ ...m, streaming: false, activeTool: null }));
      setBusy(false);
      abortRef.current = null;
    }
  };

  const handleRepoChange = (event) => {
    const newRepoId = parseInt(event.target.value);
    abortRef.current?.abort();
    setSelectedRepoId(newRepoId);
    localStorage.setItem('repo_id', newRepoId);
    setMessages([]); // a conversation is about one repo
  };

  const handleKeyDown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      ask(input);
    }
  };

  const disabled = status && !status.enabled;

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 120px)' }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 2, flexWrap: 'wrap' }}>
        <Box sx={{ flexGrow: 1 }}>
          <Typography variant="h4" fontWeight="bold">
            Ask Buma
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Ask about issues, triage decisions and team workload. Answers use this repository's live data.
          </Typography>
        </Box>
        <FormControl size="small" sx={{ minWidth: 260 }}>
          <InputLabel>Repository</InputLabel>
          <Select value={selectedRepoId || ''} label="Repository" onChange={handleRepoChange}>
            {repositories.map((repo) => (
              <MenuItem key={repo.repo_id} value={repo.repo_id}>
                {repo.repo_full_name}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
        <Button
          variant="outlined"
          startIcon={<AddCommentIcon />}
          onClick={() => {
            abortRef.current?.abort();
            setMessages([]);
          }}
          disabled={messages.length === 0}
        >
          New chat
        </Button>
      </Box>

      {pageError && <Alert severity="error" sx={{ mb: 2 }}>{pageError}</Alert>}
      {disabled && (
        <Alert severity="info" sx={{ mb: 2 }}>
          The assistant is turned off on this server. It needs ANTHROPIC_API_KEY and CHAT_ENABLED=true on the gateway.
        </Alert>
      )}

      <Card sx={{ flexGrow: 1, overflow: 'auto', mb: 2 }}>
        <CardContent>
          {messages.length === 0 ? (
            <Box sx={{ textAlign: 'center', py: 6 }}>
              <SmartToyIcon sx={{ fontSize: 56, color: '#7C3AED', mb: 1 }} />
              <Typography variant="h6" gutterBottom>
                What would you like to know{currentRepo ? ` about ${currentRepo.repo_full_name}` : ''}?
              </Typography>
              <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, justifyContent: 'center', mt: 2 }}>
                {SUGGESTIONS.map((s) => (
                  <Chip
                    key={s}
                    label={s}
                    variant="outlined"
                    onClick={() => ask(s)}
                    disabled={disabled || !selectedRepoId || busy}
                  />
                ))}
              </Box>
            </Box>
          ) : (
            messages.map((m, i) => (
              <Box
                key={i}
                sx={{ display: 'flex', justifyContent: m.role === 'user' ? 'flex-end' : 'flex-start', mb: 2 }}
              >
                <Box
                  sx={{
                    maxWidth: '80%',
                    px: 2,
                    py: 1.5,
                    borderRadius: 2,
                    backgroundColor: m.role === 'user' ? '#7C3AED' : '#f3f0ff',
                    color: m.role === 'user' ? 'white' : 'text.primary',
                  }}
                >
                  {m.role === 'assistant' && m.toolsUsed.length > 0 && (
                    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, mb: m.content ? 1 : 0 }}>
                      {[...new Set(m.toolsUsed)].map((tool) => (
                        <Chip
                          key={tool}
                          size="small"
                          label={TOOL_LABELS[tool] || tool}
                          icon={m.activeTool === tool ? <CircularProgress size={12} /> : undefined}
                          variant="outlined"
                        />
                      ))}
                    </Box>
                  )}
                  {m.content && (
                    <Typography variant="body1" sx={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                      {m.role === 'assistant' ? renderWithIssueLinks(m.content, currentRepo?.repo_full_name) : m.content}
                    </Typography>
                  )}
                  {m.streaming && !m.content && !m.activeTool && <CircularProgress size={18} />}
                  {m.error && (
                    <Alert severity="warning" sx={{ mt: m.content ? 1 : 0 }}>
                      {m.error}
                    </Alert>
                  )}
                  {m.sources.length > 0 && currentRepo && (
                    <Box sx={{ mt: 1.5, pt: 1, borderTop: '1px solid #ddd6fe' }}>
                      <Typography variant="caption" color="text.secondary">
                        Sources
                      </Typography>
                      {m.sources.map((s) => (
                        <Typography key={s.issue_number} variant="body2">
                          <Link
                            href={`https://github.com/${currentRepo.repo_full_name}/issues/${s.issue_number}`}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            #{s.issue_number}
                          </Link>{' '}
                          {s.title}
                        </Typography>
                      ))}
                    </Box>
                  )}
                </Box>
              </Box>
            ))
          )}
          <div ref={bottomRef} />
        </CardContent>
      </Card>

      <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-end' }}>
        <TextField
          fullWidth
          multiline
          maxRows={5}
          placeholder={
            disabled ? 'The assistant is disabled' : 'Ask a question… (Enter to send, Shift+Enter for a new line)'
          }
          value={input}
          onChange={(e) => setInput(e.target.value.slice(0, MAX_QUESTION_CHARS))}
          onKeyDown={handleKeyDown}
          disabled={disabled || !selectedRepoId}
          sx={{ backgroundColor: 'white' }}
        />
        {busy ? (
          <IconButton color="primary" onClick={() => abortRef.current?.abort()} aria-label="Stop">
            <StopIcon />
          </IconButton>
        ) : (
          <IconButton
            color="primary"
            onClick={() => ask(input)}
            disabled={disabled || !input.trim() || !selectedRepoId}
            aria-label="Send"
          >
            <SendIcon />
          </IconButton>
        )}
      </Box>
    </Box>
  );
}
