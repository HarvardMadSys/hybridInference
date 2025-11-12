"""User-facing UI pages for signup, login, and dashboard.

This router serves HTML pages for user authentication and API key management:
- /signup - User registration page
- /login - User login page
- /dashboard - User control panel (requires authentication)
- /verify-email-success - Email verification success page
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

# Shared CSS styles (reusing admin UI design system)
SHARED_STYLES = """
<style>
  :root {
    --bg: #0b1021;
    --panel: #121833;
    --text: #e5e7eb;
    --muted: #9aa3b2;
    --primary: #4f46e5;
    --danger: #ef4444;
    --ok: #10b981;
    --warn: #f59e0b;
    --border: #23304d;
  }
  body {
    margin: 0;
    font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, Helvetica Neue, Arial;
    background: var(--bg);
    color: var(--text);
  }
  header {
    padding: 16px 20px;
    background: #0f1530;
    border-bottom: 1px solid var(--border);
  }
  header h1 {
    margin: 0;
    font-size: 18px;
  }
  main {
    max-width: 500px;
    margin: 60px auto;
    padding: 0 16px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 24px;
    margin-bottom: 18px;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-bottom: 16px;
  }
  label {
    color: var(--muted);
    font-size: 13px;
    font-weight: 500;
  }
  input, select, textarea {
    background: #0d132b;
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 14px;
  }
  input:focus {
    outline: none;
    border-color: var(--primary);
  }
  button {
    background: var(--primary);
    color: white;
    border: none;
    border-radius: 8px;
    padding: 10px 16px;
    cursor: pointer;
    font-weight: 600;
    font-size: 14px;
    width: 100%;
  }
  button:hover {
    background: #4338ca;
  }
  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
  button.secondary {
    background: #2b3560;
  }
  button.danger {
    background: var(--danger);
  }
  .hint {
    color: var(--muted);
    font-size: 12px;
    margin-top: 4px;
  }
  .link {
    color: var(--primary);
    text-decoration: none;
  }
  .link:hover {
    text-decoration: underline;
  }
  .error {
    background: #3b0d0d;
    border-left: 3px solid var(--danger);
    padding: 12px;
    border-radius: 6px;
    margin-bottom: 16px;
    font-size: 14px;
  }
  .success {
    background: #082c1f;
    border-left: 3px solid var(--ok);
    padding: 12px;
    border-radius: 6px;
    margin-bottom: 16px;
    font-size: 14px;
  }
  .mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  }
  .text-center {
    text-align: center;
  }
  .mt-4 {
    margin-top: 16px;
  }
  .dashboard-grid {
    display: grid;
    gap: 16px;
  }
  .stat {
    display: flex;
    justify-content: space-between;
    padding: 8px 0;
    border-bottom: 1px solid var(--border);
  }
  .stat:last-child {
    border-bottom: none;
  }
  .stat-label {
    color: var(--muted);
    font-size: 13px;
  }
  .stat-value {
    font-weight: 600;
  }
  .copy-btn {
    background: #2b3560;
    padding: 6px 12px;
    font-size: 12px;
    width: auto;
    margin-left: 8px;
  }
</style>
"""


@router.get("/signup", response_class=HTMLResponse)
async def signup_page() -> str:
    """Serve user signup page."""
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Sign Up - HybridInference</title>
  {SHARED_STYLES}
</head>
<body>
  <header>
    <h1>HybridInference</h1>
  </header>
  <main>
    <div class="card">
      <h2 style="margin-top: 0;">Create Account</h2>
      <div id="error-msg" class="error" style="display: none;"></div>
      <div id="success-msg" class="success" style="display: none;"></div>

      <form id="signup-form">
        <div class="field">
          <label>Email</label>
          <input type="email" id="email" required autocomplete="email" />
        </div>

        <div class="field">
          <label>Password</label>
          <input type="password" id="password" required autocomplete="new-password" />
          <div class="hint">Min 8 characters, must contain uppercase, lowercase, and number</div>
        </div>

        <div class="field">
          <label>Name (optional)</label>
          <input type="text" id="user_name" autocomplete="name" />
        </div>

        <button type="submit" id="submit-btn">Sign Up</button>
      </form>

      <div class="hint text-center mt-4">
        Already have an account? <a href="/login" class="link">Login</a>
      </div>
    </div>
  </main>

  <script>
    const form = document.getElementById('signup-form');
    const submitBtn = document.getElementById('submit-btn');
    const errorMsg = document.getElementById('error-msg');
    const successMsg = document.getElementById('success-msg');

    form.addEventListener('submit', async (e) => {{
      e.preventDefault();

      errorMsg.style.display = 'none';
      successMsg.style.display = 'none';
      submitBtn.disabled = true;
      submitBtn.textContent = 'Creating account...';

      try {{
        const response = await fetch('/auth/signup', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{
            email: document.getElementById('email').value,
            password: document.getElementById('password').value,
            user_name: document.getElementById('user_name').value || null
          }})
        }});

        const data = await response.json();

        if (response.ok) {{
          successMsg.textContent = data.message || 'Account created successfully! Please check your email to verify your account.';
          successMsg.style.display = 'block';
          form.reset();

          // Redirect to login after 3 seconds
          setTimeout(() => {{
            window.location.href = '/login?registered=true';
          }}, 3000);
        }} else {{
          errorMsg.textContent = data.detail || 'Signup failed. Please try again.';
          errorMsg.style.display = 'block';
        }}
      }} catch (error) {{
        errorMsg.textContent = 'Network error. Please try again.';
        errorMsg.style.display = 'block';
      }} finally {{
        submitBtn.disabled = false;
        submitBtn.textContent = 'Sign Up';
      }}
    }});
  </script>
</body>
</html>
"""


@router.get("/login", response_class=HTMLResponse)
async def login_page() -> str:
    """Serve user login page."""
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Login - HybridInference</title>
  {SHARED_STYLES}
</head>
<body>
  <header>
    <h1>HybridInference</h1>
  </header>
  <main>
    <div class="card">
      <h2 style="margin-top: 0;">Login</h2>
      <div id="error-msg" class="error" style="display: none;"></div>
      <div id="success-msg" class="success" style="display: none;"></div>

      <form id="login-form">
        <div class="field">
          <label>Email</label>
          <input type="email" id="email" required autocomplete="email" />
        </div>

        <div class="field">
          <label>Password</label>
          <input type="password" id="password" required autocomplete="current-password" />
        </div>

        <button type="submit" id="submit-btn">Login</button>
      </form>

      <div class="hint text-center mt-4">
        Don't have an account? <a href="/signup" class="link">Sign Up</a>
      </div>
    </div>
  </main>

  <script>
    // Check for registration success message
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('registered') === 'true') {{
      const successMsg = document.getElementById('success-msg');
      successMsg.textContent = 'Account created successfully! Please login.';
      successMsg.style.display = 'block';
    }}

    const form = document.getElementById('login-form');
    const submitBtn = document.getElementById('submit-btn');
    const errorMsg = document.getElementById('error-msg');

    form.addEventListener('submit', async (e) => {{
      e.preventDefault();

      errorMsg.style.display = 'none';
      submitBtn.disabled = true;
      submitBtn.textContent = 'Logging in...';

      try {{
        const response = await fetch('/auth/login', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          credentials: 'include',  // Important for cookies
          body: JSON.stringify({{
            email: document.getElementById('email').value,
            password: document.getElementById('password').value
          }})
        }});

        const data = await response.json();

        if (response.ok) {{
          // Store access token in sessionStorage
          sessionStorage.setItem('access_token', data.access_token);

          // Redirect to dashboard
          window.location.href = '/dashboard';
        }} else {{
          errorMsg.textContent = data.detail || 'Login failed. Please check your credentials.';
          errorMsg.style.display = 'block';
        }}
      }} catch (error) {{
        errorMsg.textContent = 'Network error. Please try again.';
        errorMsg.style.display = 'block';
      }} finally {{
        submitBtn.disabled = false;
        submitBtn.textContent = 'Login';
      }}
    }});
  </script>
</body>
</html>
"""


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page() -> str:
    """Serve user dashboard page."""
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Dashboard - HybridInference</title>
  {SHARED_STYLES}
  <style>
    main {{
      max-width: 900px;
    }}
  </style>
</head>
<body>
  <header>
    <div style="display: flex; justify-content: space-between; align-items: center;">
      <h1>HybridInference Dashboard</h1>
      <button id="logout-btn" class="secondary" style="width: auto; padding: 8px 16px;">Logout</button>
    </div>
  </header>
  <main>
    <div id="loading" class="card text-center">
      <p>Loading...</p>
    </div>

    <div id="content" style="display: none;">
      <!-- User Info Card -->
      <div class="card">
        <h3 style="margin-top: 0;">Account Information</h3>
        <div id="user-info"></div>
      </div>

      <!-- API Key Card -->
      <div class="card">
        <h3 style="margin-top: 0;">API Key</h3>
        <div id="api-key-display"></div>
        <div id="api-key-actions" style="margin-top: 16px;"></div>
      </div>

      <!-- Usage Card -->
      <div class="card">
        <h3 style="margin-top: 0;">Usage Statistics (Today)</h3>
        <div id="usage-stats"></div>
      </div>
    </div>
  </main>

  <script>
    const token = sessionStorage.getItem('access_token');
    if (!token) {{
      window.location.href = '/login';
    }}

    async function api(path, options = {{}}) {{
      const response = await fetch(path, {{
        ...options,
        headers: {{
          'Authorization': `Bearer ${{token}}`,
          'Content-Type': 'application/json',
          ...options.headers
        }},
        credentials: 'include'
      }});

      if (response.status === 401) {{
        // Try to refresh token
        const refreshResponse = await fetch('/auth/refresh', {{
          method: 'POST',
          credentials: 'include'
        }});

        if (refreshResponse.ok) {{
          const data = await refreshResponse.json();
          sessionStorage.setItem('access_token', data.access_token);
          // Retry original request
          return fetch(path, {{
            ...options,
            headers: {{
              'Authorization': `Bearer ${{data.access_token}}`,
              'Content-Type': 'application/json',
              ...options.headers
            }},
            credentials: 'include'
          }});
        }}

        // Refresh failed, redirect to login
        sessionStorage.removeItem('access_token');
        window.location.href = '/login';
        throw new Error('Authentication failed');
      }}

      return response;
    }}

    async function loadDashboard() {{
      try {{
        // Load user info
        const userResponse = await api('/user/me');
        const user = await userResponse.json();

        document.getElementById('user-info').innerHTML = `
          <div class="stat">
            <span class="stat-label">Email</span>
            <span class="stat-value">${{user.email}}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Tier</span>
            <span class="stat-value">${{user.tier}}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Email Verified</span>
            <span class="stat-value">${{user.email_verified ? 'Yes' : 'No'}}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Member Since</span>
            <span class="stat-value">${{new Date(user.created_at).toLocaleDateString()}}</span>
          </div>
        `;

        // Load API key info
        try {{
          const keyResponse = await api('/user/api-keys');
          const apiKey = await keyResponse.json();

          document.getElementById('api-key-display').innerHTML = `
            <div class="stat">
              <span class="stat-label">API Key</span>
              <span class="stat-value mono">${{apiKey.key_masked}}</span>
            </div>
            <div class="stat">
              <span class="stat-label">Created</span>
              <span class="stat-value">${{new Date(apiKey.created_at).toLocaleDateString()}}</span>
            </div>
            <div class="stat">
              <span class="stat-label">Last Used</span>
              <span class="stat-value">${{apiKey.last_used_at ? new Date(apiKey.last_used_at).toLocaleString() : 'Never'}}</span>
            </div>
          `;

          document.getElementById('api-key-actions').innerHTML = `
            <button class="danger" onclick="regenerateKey()">Regenerate API Key</button>
          `;
        }} catch (error) {{
          if (error.message.includes('404')) {{
            document.getElementById('api-key-display').innerHTML = `
              <p class="hint">No API key found. ${{user.email_verified ? 'Click below to generate one.' : 'Please verify your email first.'}}</p>
            `;

            if (user.email_verified) {{
              document.getElementById('api-key-actions').innerHTML = `
                <button onclick="createKey()">Generate API Key</button>
              `;
            }}
          }}
        }}

        // Load usage stats
        const usageResponse = await api('/user/usage?period=today');
        const usage = await usageResponse.json();

        document.getElementById('usage-stats').innerHTML = `
          <div class="stat">
            <span class="stat-label">Daily Quota</span>
            <span class="stat-value">$${{usage.quota.spent_today_usd.toFixed(2)}} / $${{usage.quota.daily_limit_usd.toFixed(2)}}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Remaining Today</span>
            <span class="stat-value">$${{usage.quota.remaining_today_usd.toFixed(2)}}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Requests Today</span>
            <span class="stat-value">${{usage.usage.requests}}</span>
          </div>
          <div class="stat">
            <span class="stat-label">Tokens Used</span>
            <span class="stat-value">${{(usage.usage.prompt_tokens + usage.usage.completion_tokens).toLocaleString()}}</span>
          </div>
        `;

        document.getElementById('loading').style.display = 'none';
        document.getElementById('content').style.display = 'block';
      }} catch (error) {{
        console.error('Failed to load dashboard:', error);
        alert('Failed to load dashboard. Please try again.');
      }}
    }}

    async function createKey() {{
      if (!confirm('Generate a new API key? This can only be done once.')) return;

      try {{
        const response = await api('/user/api-keys', {{ method: 'POST' }});
        const data = await response.json();

        if (response.ok) {{
          alert(`API Key created successfully!\\n\\n${{data.api_key}}\\n\\nSave this key now. It cannot be retrieved later.`);
          loadDashboard();
        }} else {{
          alert(data.detail || 'Failed to create API key');
        }}
      }} catch (error) {{
        alert('Failed to create API key. Please try again.');
      }}
    }}

    async function regenerateKey() {{
      if (!confirm('Regenerate API key? Your old key will be immediately invalidated.')) return;

      try {{
        const response = await api('/user/api-keys/regenerate', {{ method: 'POST' }});
        const data = await response.json();

        if (response.ok) {{
          alert(`New API Key generated!\\n\\n${{data.api_key}}\\n\\nSave this key now. It cannot be retrieved later.\\n\\nOld key (${{data.old_key_prefix}}) has been revoked.`);
          loadDashboard();
        }} else {{
          alert(data.detail || 'Failed to regenerate API key');
        }}
      }} catch (error) {{
        alert('Failed to regenerate API key. Please try again.');
      }}
    }}

    document.getElementById('logout-btn').addEventListener('click', async () => {{
      try {{
        await api('/auth/logout', {{ method: 'POST' }});
      }} catch (error) {{
        // Ignore errors
      }} finally {{
        sessionStorage.removeItem('access_token');
        window.location.href = '/login';
      }}
    }});

    loadDashboard();
  </script>
</body>
</html>
"""


@router.get("/verify-email-success", response_class=HTMLResponse)
async def verify_email_success_page() -> str:
    """Serve email verification success page."""
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Email Verified - HybridInference</title>
  {SHARED_STYLES}
</head>
<body>
  <header>
    <h1>HybridInference</h1>
  </header>
  <main>
    <div class="card text-center">
      <h2 style="margin-top: 0;">Email Verified!</h2>
      <div class="success">
        Your email has been successfully verified.
      </div>
      <p class="hint">You can now login and generate your API key.</p>
      <a href="/login">
        <button>Go to Login</button>
      </a>
    </div>
  </main>
</body>
</html>
"""
