import React from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { ThemeProvider, createTheme } from '@mui/material/styles';
import CssBaseline from '@mui/material/CssBaseline';

import Login from './pages/Login';
import AuthCallback from './pages/AuthCallback';
import Layout from './components/Layout/Layout';
import Dashboard from './pages/Dashboard';
import Team from './pages/Team';
import Repositories from './pages/Repositories';
import RepositorySetup from './pages/RepositorySetup';
import ProtectedRoute from './components/ProtectedRoute';
import Productivity from './pages/Productivity';
import Issues from './pages/Issues';
import Assistant from './pages/Assistant';

const theme = createTheme({
  palette: {
    primary: {
      main: '#7C3AED',
    },
  },
});

function App() {
  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <BrowserRouter>
        <Routes>
          {/* Public routes */}
          <Route path="/login" element={<Login />} />
          <Route path="/auth/callback" element={<AuthCallback />} />
          
          {/* Protected routes */}
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <Layout />
              </ProtectedRoute>
            }
          >
            <Route index element={<Navigate to="/dashboard" />} />
            <Route path="dashboard" element={<Dashboard />} />
            <Route path="team" element={<Team />} />
            <Route path="repositories" element={<Repositories />} />
            <Route path="productivity" element={<Productivity />} />
            <Route path="setup" element={<RepositorySetup />} />
            <Route path="issues" element={<Issues />} />
            <Route path="assistant" element={<Assistant />} />
            <Route path="analytics" element={<div>Analytics - Coming Soon</div>} />
            <Route path="settings" element={<div>Settings - Coming Soon</div>} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ThemeProvider>
  );
}

export default App;