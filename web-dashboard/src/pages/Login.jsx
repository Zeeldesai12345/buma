import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { 
  Box, 
  Card, 
  CardContent, 
  TextField, 
  Button, 
  Typography,
  Alert,
  Divider,
  CircularProgress,
  AppBar,
  Toolbar
} from '@mui/material';
import GitHubIcon from '@mui/icons-material/GitHub';
import AccessTimeIcon from '@mui/icons-material/AccessTime';
import { authService } from '../services/auth';

const API_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000';

export default function Login() {
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');
  const [loading, setLoading] = useState(false);

  const handleLogin = async (e) => {
    e.preventDefault();
    setError('');
    setInfo('');
    setLoading(true);

    try {
      const result = await authService.login(email, password);
      
      if (result.success) {
        if (result.isMock) {
          // Show info message, DON'T auto-redirect
          setInfo('ℹ️ Development Mode: Backend email login not available. Using mock authentication.');
          // User must click Continue button to proceed
        } else {
          // Real backend login - redirect immediately
          navigate('/dashboard');
        }
      } else {
        setError(result.error);
      }
    } catch (err) {
      setError('Login failed. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  const handleGitHubLogin = () => {
    window.location.href = `${API_URL}/auth/github`;
  };

  return (
    <Box sx={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
      {/* Header */}
      <AppBar 
        position="static" 
        elevation={0}
        sx={{ 
          backgroundColor: 'white',
          borderBottom: '1px solid #e0e0e0'
        }}
      >
        <Toolbar>
          <Typography 
            variant="h5" 
            sx={{ 
              flexGrow: 1, 
              color: '#7C3AED',
              fontWeight: 'bold'
            }}
          >
            Buma
          </Typography>
          <Box sx={{ display: 'flex', alignItems: 'center', color: '#666' }}>
            <AccessTimeIcon sx={{ mr: 1, fontSize: 20 }} />
            <Typography variant="body2">
              {new Date().toLocaleString('en-US', { 
                month: '2-digit', 
                day: '2-digit', 
                year: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
              })}
            </Typography>
          </Box>
        </Toolbar>
      </AppBar>

      {/* Main Content */}
      <Box
        sx={{
          flexGrow: 1,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          background: 'linear-gradient(135deg, #1a1a1a 0%, #2d2d2d 100%)',
          position: 'relative',
          overflow: 'hidden',
          padding: '0 100px',
        }}
      >
        {/* Background Pattern */}
        <Box
          sx={{
            position: 'absolute',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            backgroundImage: 'url("data:image/svg+xml,%3Csvg width=\'100\' height=\'100\' xmlns=\'http://www.w3.org/2000/svg\'%3E%3Cpath d=\'M0 50 Q25 30, 50 50 T100 50\' stroke=\'%2322c55e\' stroke-width=\'2\' fill=\'none\' opacity=\'0.3\'/%3E%3C/svg%3E")',
            opacity: 0.1,
          }}
        />

        {/* Left - Headline */}
        <Box sx={{ maxWidth: 500, color: 'white', zIndex: 1 }}>
          <Typography 
            variant="h2" 
            sx={{ 
              fontWeight: 'bold',
              lineHeight: 1.2,
              mb: 2
            }}
          >
            Automate Bug Triaging With{' '}
            <Box component="span" sx={{ color: '#7C3AED' }}>
              Intelligence
            </Box>
          </Typography>
        </Box>

        {/* Right - Login Card */}
        <Card 
          sx={{ 
            width: '100%', 
            maxWidth: 440,
            boxShadow: '0 8px 32px rgba(0,0,0,0.3)',
            borderRadius: 2,
            zIndex: 1
          }}
        >
          <CardContent sx={{ p: 4 }}>
            <Typography 
              variant="h4" 
              gutterBottom 
              align="center"
              fontWeight="bold"
            >
              Login
            </Typography>
            <Typography 
              variant="body2" 
              color="text.secondary" 
              align="center"  
              sx={{ mb: 3 }}
            >
              Automate Bug Triaging With Intelligence
            </Typography>

            {/* Error Alert */}
            {error && (
              <Alert severity="error" sx={{ mb: 2 }}>
                {error}
              </Alert>
            )}

            {/* Info Alert with Continue Button */}
            {info && (
              <>
                <Alert severity="info" sx={{ mb: 2 }}>
                  {info}
                </Alert>
                
                {/* Continue to Dashboard Button */}
                <Button
                  fullWidth
                  variant="contained"
                  onClick={() => navigate('/dashboard')}
                  sx={{ 
                    mb: 2,
                    py: 1.5,
                    backgroundColor: '#7C3AED',
                    textTransform: 'none',
                    fontSize: '15px',
                    fontWeight: 'bold',
                    '&:hover': {
                      backgroundColor: '#6D28D9'
                    }
                  }}
                >
                  Continue to Dashboard
                </Button>
              </>
            )}

            {/* GitHub Login */}
            <Button
              fullWidth
              variant="outlined"
              size="large"
              startIcon={<GitHubIcon />}
              onClick={handleGitHubLogin}
              sx={{ 
                mb: 2,
                py: 1.5,
                borderColor: '#333',
                color: '#333',
                textTransform: 'none',
                fontSize: '15px',
                '&:hover': {
                  borderColor: '#000',
                  backgroundColor: 'rgba(0,0,0,0.04)',
                }
              }}
            >
              SIGN IN WITH GITHUB
            </Button>

            <Divider sx={{ my: 3 }}>
              <Typography variant="caption" color="text.secondary">
                OR
              </Typography>
            </Divider>

            {/* Email/Password Form */}
            <form onSubmit={handleLogin}>
              <TextField
                fullWidth
                label="Email Address *"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                margin="normal"
                required
                disabled={loading}
                placeholder="admin@test.com"
                sx={{ mb: 2 }}
              />
              <TextField
                fullWidth
                label="Password *"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                margin="normal"
                required
                disabled={loading}
                placeholder="••••••••"
                sx={{ mb: 3 }}
              />
              
              <Button
                fullWidth
                variant="contained"
                type="submit"
                size="large"
                sx={{ 
                  py: 1.5,
                  backgroundColor: '#7C3AED',
                  textTransform: 'none',
                  fontSize: '15px',
                  fontWeight: 'bold',
                  '&:hover': {
                    backgroundColor: '#6D28D9'
                  }
                }}
                disabled={loading}
              >
                {loading ? <CircularProgress size={24} color="inherit" /> : 'SIGN IN'}
              </Button>
            </form>

            {/* Dev Info */}
            <Box 
              sx={{ 
                mt: 3, 
                p: 2, 
                backgroundColor: '#f8f9fa', 
                borderRadius: 1,
                border: '1px solid #e0e0e0'
              }}
            >
              <Typography variant="caption" color="text.secondary">
                <strong>For Development:</strong>
                <br />
                GitHub OAuth (production) or test: admin@test.com / admin123
              </Typography>
            </Box>
          </CardContent>
        </Card>
      </Box>
    </Box>
  );
}