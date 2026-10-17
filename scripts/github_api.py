"""One public-safe control issue; no Terraform files are ever uploaded."""

import json
import os
import re
import urllib.error
import urllib.request

from scripts.safety import Blocked, require

MARKER = '<!-- oracle-vps-control:v1 -->'
STATE_RE = re.compile(r'<!-- state:(\{.*?\}) -->', re.DOTALL)


class GitHub:
    def __init__(self):
        self.repo = os.environ['GH_REPO']
        require(bool(re.fullmatch(r'[\w.-]+/[\w.-]+', self.repo)), 'Repositório inválido.')
        self.token = os.environ['GH_TOKEN']
        self.issue = None
        self.run_url = os.environ.get('RUN_URL', f'https://github.com/{self.repo}/actions')
        self.user = os.environ.get('NOTIFY_USER', self.repo.split('/')[0])
        require(bool(re.fullmatch(r'[\w-]+', self.user)), 'Usuário de aviso inválido.')

    def request(self, method, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(
            f'https://api.github.com/repos/{self.repo}{path}', data=body, method=method,
            headers={'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                data = response.read()
                return json.loads(data) if data else None
        except (urllib.error.URLError, TimeoutError, ValueError):
            # Never echo request headers, API bodies or secrets.
            raise Blocked('Falha ao acessar GitHub; nenhuma nova operação deve ser iniciada.') from None

    def assert_public(self):
        repo = self.request('GET', '')
        require(repo.get('visibility') == 'public' and not repo.get('private'),
                'Custo zero exige repositório público e runner padrão.')
        require(repo.get('has_issues'), 'Issues precisam estar habilitadas.')

    def load(self):
        found = []
        page = 1
        while True:
            issues = self.request('GET', f'/issues?state=all&per_page=100&page={page}')
            for issue in issues:
                if 'pull_request' not in issue and MARKER in (issue.get('body') or ''):
                    require(issue.get('user', {}).get('login') == 'github-actions[bot]',
                            'Issue de controle deve pertencer ao bot do GitHub Actions.')
                    found.append(issue)
            if len(issues) < 100:
                break
            page += 1
        require(len(found) <= 1, 'Mais de uma Issue de controle; revisão necessária.')
        if not found:
            return None
        self.issue = found[0]
        match = STATE_RE.search(self.issue.get('body') or '')
        require(match is not None, 'Estado da Issue de controle ausente.')
        try:
            state = json.loads(match.group(1))
        except ValueError:
            raise Blocked('Estado da Issue inválido; revisão necessária.') from None
        require(state.get('version') == 1 and state.get('phase') in
                ('ready', 'pending', 'tracking', 'blocked', 'complete'), 'Estado desconhecido.')
        require(isinstance(state.get('seq'), int) and state['seq'] >= 0, 'Contador inválido.')
        return state

    def save(self, state):
        phase = state['phase']
        body = (f'{MARKER}\n\nAutomação Oracle Always Free. Estado: **{phase}**.\n\n'
                'Use **Subscribe** nesta Issue e habilite e-mail nas suas notificações do GitHub.\n'
                'Detalhes da máquina e credenciais ficam na Oracle, fora desta Issue pública.\n\n'
                f'[Execução mais recente]({self.run_url})\n\n'
                f'<!-- state:{json.dumps(state, sort_keys=True, separators=(",", ":"))} -->')
        payload = {'title': 'Acompanhamento da VPS Oracle Always Free', 'body': body}
        if self.issue:
            self.issue = self.request('PATCH', f'/issues/{self.issue["number"]}', payload)
        else:
            self.issue = self.request('POST', '/issues', payload)

    def notify(self, key, text):
        marker = f'<!-- oracle-notice:{key} -->'
        page = 1
        while True:
            comments = self.request('GET', f'/issues/{self.issue["number"]}/comments?per_page=100&page={page}')
            if any(marker in x.get('body', '') and x.get('user', {}).get('login') == 'github-actions[bot]'
                   for x in comments):
                return
            if len(comments) < 100:
                break
            page += 1
        self.request('POST', f'/issues/{self.issue["number"]}/comments', {
            'body': f'{marker}\n@{self.user} {text}\n\n[Ver execução]({self.run_url})'})

    def disable(self):
        self.request('PUT', '/actions/workflows/oracle-vps.yml/disable')
