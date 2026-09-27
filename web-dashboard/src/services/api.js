import axios from 'axios';

const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000';

const api = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Add auth token to requests
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// CONFIG APIs
export const configApi = {
  // Repository management
  getAllRepos: (params = {}) => api.get('/api/config/repos', { params }), // ← ADDED
  enrollRepo: (data) => api.post('/api/config/repos', data),
  getRepo: (repoId) => api.get(`/api/config/repos/${repoId}`),
  updateRepo: (repoId, data) => api.patch(`/api/config/repos/${repoId}`, data),
  
  // Developer management
  addDeveloper: (repoId, data) => api.post(`/api/config/repos/${repoId}/developers`, data),
  updateDeveloper: (repoId, githubLogin, data) => 
    api.patch(`/api/config/repos/${repoId}/developers/${githubLogin}`, data),
  deleteDeveloper: (repoId, githubLogin) => 
    api.delete(`/api/config/repos/${repoId}/developers/${githubLogin}`),
};

// OBSERVABILITY APIs
export const observabilityApi = {
  getTriageHistory: (repoId, params = {}) => 
    api.get(`/api/triage/${repoId}`, { params }),
  getWorkload: (repoId) => api.get(`/api/workload/${repoId}`),
  getProductivity: (repoId, window = '30d') => 
    api.get(`/api/productivity/${repoId}`, { params: { window } }),
  getIssues: (repoId, params = {}) => 
    api.get(`/api/issues/${repoId}`, { params }),
};



// CHAT ("Ask Buma") APIs
export const chatApi = {
  getStatus: () => api.get('/api/chat/status'),

  // Streams one answer as Server-Sent Events. axios can't read a streamed POST body in the
  // browser, so this uses fetch. Calls onEvent({type, ...}) for each event; resolves when the
  // stream ends. Rejects with an Error carrying `status` for non-2xx responses.
  ask: async (repoId, message, history, onEvent, signal) => {
    const token = localStorage.getItem('token');
    const response = await fetch(`${API_BASE_URL}/api/chat/${repoId}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ message, history }),
      signal,
    });

    if (!response.ok) {
      let detail = `Request failed (${response.status})`;
      try {
        const body = await response.json();
        if (typeof body.detail === 'string') detail = body.detail;
      } catch {
        // non-JSON error body; keep the generic message
      }
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split('\n\n');
      buffer = frames.pop();
      for (const frame of frames) {
        if (frame.startsWith('data: ')) {
          onEvent(JSON.parse(frame.slice(6)));
        }
      }
    }
  },
};

export const healthCheck = () => api.get('/health');

export default api;