// modelmatch-backend CI/CD pipeline (P18; test taxonomy refined in P31). Multibranch
// job on the persistent Jenkins controller. Two ordered stage groups + a release tail:
//   FAST    — Build -> Bandit/pip-audit gate -> unit test (no containers, fake LLM).
//   FULL    — Package BE image -> Trivy -> DB-backed contract tests (pytest, in-process)
//             -> Container Integration (BE candidate image ↔ Postgres, HTTP smoke) ->
//             E2E (compose, full stack) -> [main only] e2e-live (one real Nova call).
//   RELEASE — [main] Tag (SemVer) -> Publish (ECR) -> Deploy (gitops image-tag bump).
// FAST runs on every push; FULL runs on feature/* and main only (other branches stop after FAST);
// e2e-live + RELEASE run on main only; FAST_ONLY skips FULL on demand.
// No static AWS keys: EC2 instance role does ECR + Bedrock; deploy keys push tag/bump.
// Non-secret delivery config lives in ci/pipeline.env. Tools (uv/Playwright/Trivy/yq) run
// as pinned containers since the box only has Docker.

// Make an id safe for docker tags + compose project names.
def sanitizeId(String s) {
  String out = s.toLowerCase().replace('%2f', '-')
  out = out.replaceAll('[^a-z0-9_-]', '-').replaceAll('-+', '-')
  out = out.replaceAll('^[-_]+', '').replaceAll('[-_]+$', '')
  return out
}

// Run a uv/python command in the pinned uv container with the workspace mounted, so the
// Build .venv persists across Python stages. Runs as the Jenkins uid; HOME/cache in /tmp.
def runUv(String cmd) {
  sh """
    docker run --rm \\
      -u \$(id -u):\$(id -g) \\
      -e HOME=/tmp -e UV_CACHE_DIR=/tmp/.uv-cache \\
      -v "\$WORKSPACE":/work -w /work \\
      "\$UV_IMAGE" \\
      bash -lc '${cmd}'
  """
}

// Read a piece of git metadata for the notification with a SOFT fallback: a missing
// .git (checkout failed) or a failed git command yields '' instead of throwing — so
// the Slack notification still fires even on a pre-checkout / early failure.
def gitFact(String cmd) {
  return sh(script: "(${cmd}) 2>/dev/null || true", returnStdout: true).trim()
}

// Wrap slackSend so a missing/misconfigured Slack plugin doesn't flip a green build
// red from a post.success throw — log + continue instead. A post.success that throws
// is treated as a stage failure; we'd rather lose the notification than the build.
def slackOrLog(Map args) {
  try {
    slackSend(args)
  } catch (Throwable t) {
    echo "WARN: slackSend failed — ${t.class.simpleName}: ${t.message?.take(200) ?: '(no message)'}"
  }
}

// Slack notification mirroring the toxictypo template, adapted for GitHub (commit URL
// is `<repo>/commit/<sha>`, not GitLab's `/-/commit/`; no updateGitlabCommitStatus).
// `env.FAILED_STAGE` is set at the start of every stage so the failure message can
// name + link to the stage that broke. Tolerant of git-metadata failures — an early
// checkout failure still surfaces a job/build/stage Slack notification.
def notifySlack(boolean ok) {
  def branchName  = env.BRANCH_NAME ?: (env.JOB_NAME ? env.JOB_NAME.replaceAll('%2F', '/') : 'unknown')
  def pushedBy    = gitFact('git log -1 --pretty=format:"%an"') ?: 'unknown'
  def shortCommit = gitFact('git rev-parse --short=7 HEAD')      ?: 'unknown'
  def fullCommit  = gitFact('git rev-parse HEAD')
  def commitMsg   = gitFact('git log -1 --pretty=format:"%s"')   ?: '(commit message unavailable)'
  def repoUrl     = gitFact('git remote get-url origin')
    .replace('git@github.com:', 'https://github.com/')
    .replaceAll(/\.git$/, '')
  // Hide the commit link entirely when either piece is missing — a dangling Markdown
  // link would render badly in Slack.
  def commitDisplay = (repoUrl && fullCommit) ? "<${repoUrl}/commit/${fullCommit}|${shortCommit}>" : shortCommit
  def jobInfo = "${env.JOB_NAME ?: 'unknown-job'} #${env.BUILD_NUMBER ?: '?'}"
  if (ok) {
    slackOrLog channel: '#jenkins-steve', color: 'good', message: """\
✅ Build passed (${jobInfo})
Branch: ${branchName}
Commit: ${commitDisplay}
Commit message: "${commitMsg}"
Pushed by: ${pushedBy}"""
  } else {
    def stageName = env.FAILED_STAGE ?: 'unknown'
    def failedStageDisplay = env.BUILD_URL ? "<${env.BUILD_URL}console|${stageName}>" : stageName
    slackOrLog channel: '#jenkins-steve', color: 'danger', message: """\
❌ Build failed (${jobInfo})
Branch: ${branchName}
Commit: ${commitDisplay}
Commit message: "${commitMsg}"
Pushed by: ${pushedBy}
Failed Stage: ${failedStageDisplay}"""
  }
}

pipeline {
  agent any

  parameters {
    // Manual override to run only the FAST lane (e.g. a quick demo build).
    booleanParam(name: 'FAST_ONLY', defaultValue: false,
                 description: 'Run only the fast lane (static gate + unit). Default: false (run everything).')
  }

  options {
    timestamps()
    // We do our own clean checkout below (CleanBeforeCheckout), so skip Jenkins' implicit one.
    skipDefaultCheckout true
    // Serialize same-branch builds so the compose stack + tag push don't race themselves
    // (cross-branch isolation comes from the unique RUN_ID).
    disableConcurrentBuilds()
    timeout(time: 40, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '20'))
  }

  stages {
    stage('Source + config') {
      steps {
        script {
          env.FAILED_STAGE = 'Source + config'
          // Clean checkout: CleanBeforeCheckout wipes the workspace first; reuse the
          // Multibranch job's branch + deploy key. Capture scmVars for GIT_COMMIT (not
          // pre-populated under skipDefaultCheckout).
          def scmVars = checkout([
            $class: 'GitSCM',
            branches: scm.branches,
            extensions: [[$class: 'CleanBeforeCheckout']],
            userRemoteConfigs: scm.userRemoteConfigs,
          ])
          // Load non-secret CI config. Parse with collectEntries (sandbox-safe), then assign
          // env by explicit property — the CPS sandbox rejects dynamic env[k]=v.
          Map cfg = readFile('ci/pipeline.env').readLines()
            .findAll { String l -> l.trim() && !l.trim().startsWith('#') && l.contains('=') }
            .collectEntries { String l ->
              int i = l.indexOf('=')
              [(l.substring(0, i).trim()): l.substring(i + 1).trim()]
            }
          def required = [
            'AWS_DEFAULT_REGION', 'ECR_REGISTRY', 'ECR_REPO',
            'E2E_FRONTEND_REPO', 'E2E_FRONTEND_TAG',
            'UV_IMAGE', 'POSTGRES_IMAGE', 'PLAYWRIGHT_IMAGE', 'TRIVY_IMAGE', 'YQ_IMAGE',
            'BE_REPO_SSH', 'GITOPS_REPO', 'GITOPS_VALUES', 'GITOPS_MIGRATE_VALUES',
            'CRED_BE_DEPLOY_KEY', 'CRED_GITOPS_KEY',
          ]
          def missing = required.findAll { !cfg.get(it) }
          if (missing) { error "ci/pipeline.env missing required keys: ${missing.join(', ')}" }

          env.AWS_DEFAULT_REGION = cfg.get('AWS_DEFAULT_REGION')
          env.ECR_REGISTRY       = cfg.get('ECR_REGISTRY')
          env.ECR_REPO           = cfg.get('ECR_REPO')
          env.E2E_FRONTEND_REPO  = cfg.get('E2E_FRONTEND_REPO')
          env.E2E_FRONTEND_TAG   = cfg.get('E2E_FRONTEND_TAG')
          env.UV_IMAGE           = cfg.get('UV_IMAGE')
          env.POSTGRES_IMAGE     = cfg.get('POSTGRES_IMAGE')
          env.PLAYWRIGHT_IMAGE   = cfg.get('PLAYWRIGHT_IMAGE')
          env.TRIVY_IMAGE        = cfg.get('TRIVY_IMAGE')
          env.YQ_IMAGE           = cfg.get('YQ_IMAGE')
          env.BE_REPO_SSH        = cfg.get('BE_REPO_SSH')
          env.GITOPS_REPO        = cfg.get('GITOPS_REPO')
          env.GITOPS_VALUES      = cfg.get('GITOPS_VALUES')
          env.GITOPS_MIGRATE_VALUES = cfg.get('GITOPS_MIGRATE_VALUES')
          env.CRED_BE_DEPLOY_KEY = cfg.get('CRED_BE_DEPLOY_KEY')
          env.CRED_GITOPS_KEY    = cfg.get('CRED_GITOPS_KEY')

          // Globally-unique run id (BUILD_NUMBER is per-branch in Multibranch).
          String job = sanitizeId(env.JOB_NAME)
          if (job.length() > 50) { job = job.substring(0, 50).replaceAll('[-_]+$', '') }
          // Commit SHA from the checkout return; fall back to HEAD.
          String gc = (scmVars?.GIT_COMMIT ?: '').trim()
          if (!gc) { gc = sh(returnStdout: true, script: 'git rev-parse HEAD').trim() }
          env.GIT_COMMIT = gc
          String sha = gc.length() >= 7 ? gc.substring(0, 7) : gc
          env.RUN_ID = "${job}-${env.BUILD_NUMBER}-${sha}"
          env.IMAGE_CANDIDATE = "candidate-${env.RUN_ID}"

          sh 'git --no-pager log -1 --oneline; echo "Branch: ${BRANCH_NAME}  RunId: ${RUN_ID}  fastOnly: ${FAST_ONLY}"'
        }
      }
    }

    // ===================== FAST lane (every push) =====================
    // Quick feedback: deps, security gate, unit suite — no containers.

    stage('Fast lane') {
      stages {
        stage('Build (uv sync)') {
          // Resolve locked deps into a workspace .venv once; later stages reuse it (--no-sync).
          steps {
            script { env.FAILED_STAGE = 'Build (uv sync)' }
            runUv('uv sync --frozen --extra bedrock')
          }
        }

        stage('Static/dep gate (Bandit + pip-audit)') {
          steps {
            script { env.FAILED_STAGE = 'Static/dep gate (Bandit + pip-audit)' }
            // Bandit SAST over app + agent; hard gate on HIGH severity AND HIGH confidence.
            runUv('uv run --no-sync bandit -r app agent --severity-level high --confidence-level high -q')
            // pip-audit over the runtime deps only (the set the image ships). Remediate-not-
            // waive: any advisory fails; documented unfixables would go in --ignore-vuln.
            runUv('uv export --frozen --no-dev --extra bedrock --format requirements-txt --no-emit-project -o /tmp/req.txt && uv run --no-sync pip-audit -r /tmp/req.txt --progress-spinner=off')
          }
        }

        stage('Test (unit, no containers)') {
          // Unit suite only (-m "not integration" → no Postgres). Fake LLM, zero tokens.
          steps {
            script { env.FAILED_STAGE = 'Test (unit, no containers)' }
            runUv('LLM_CLIENT=fake uv run --no-sync pytest -m "not integration" -q')
          }
        }
      }
    }

    // ===================== FULL lane (every push) =====================
    // Build + scan the image, then integration + E2E.

    stage('Full lane') {
      // Branches other than main / feature/* stop after FAST — keeps throwaway branches cheap.
      when {
        allOf {
          expression { !params.FAST_ONLY }
          anyOf {
            branch 'main'
            branch pattern: 'feature/*', comparator: 'GLOB'
          }
        }
      }
      stages {
        stage('Package (BE image)') {
          // Build the backend image with the per-build candidate tag; promoted to SemVer on main.
          steps {
            script { env.FAILED_STAGE = 'Package (BE image)' }
            sh 'docker build -t "$ECR_REGISTRY/$ECR_REPO:$IMAGE_CANDIDATE" .'
          }
        }

        stage('Trivy image scan') {
          // Gate on fixable CRITICAL + HIGH (--ignore-unfixed) so unpatched base-OS CVEs
          // don't permanently block; .trivyignore holds documented per-CVE waivers (none yet).
          steps {
            script { env.FAILED_STAGE = 'Trivy image scan' }
            sh '''
              set -eu
              docker run --rm \
                -v /var/run/docker.sock:/var/run/docker.sock \
                -v "$WORKSPACE/.trivyignore":/.trivyignore:ro \
                "$TRIVY_IMAGE" image \
                  --scanners vuln \
                  --severity CRITICAL,HIGH \
                  --ignore-unfixed \
                  --ignorefile /.trivyignore \
                  --exit-code 1 \
                  --no-progress \
                  "$ECR_REGISTRY/$ECR_REPO:$IMAGE_CANDIDATE"
            '''
          }
        }

        stage('DB-backed contract tests (pytest)') {
          // Fast in-process API + ORM coverage against a real Postgres from a throwaway
          // compose `db` (conftest creates its DB + runs alembic). The TEST CODE imports
          // app modules directly — there is NO candidate image involved here; this is
          // *contract* coverage, not the container integration gate (see next stage).
          // Fake LLM. Never the cluster/dev DB.
          steps {
            script {
              env.FAILED_STAGE = 'DB-backed contract tests (pytest)'
              String pgPort = sh(script: './ci/free-ports.sh 1', returnStdout: true).trim()
              String jwt = sh(script: 'openssl rand -hex 32', returnStdout: true).trim()
              withEnv([
                "COMPOSE_PROJECT_NAME=mm-be-contract-${env.RUN_ID}",
                "BACKEND_IMAGE=${env.ECR_REGISTRY}/${env.ECR_REPO}:${env.IMAGE_CANDIDATE}",
                "POSTGRES_PORT=${pgPort}",
                "JWT_SECRET=${jwt}",
              ]) {
                sh './ci/e2e-stack.sh up-db'
                // pytest in the uv container on host network so localhost:<pgPort> reaches compose.
                sh """
                  set -eu
                  docker run --rm --network host \
                    -u "\$(id -u):\$(id -g)" -e HOME=/tmp -e UV_CACHE_DIR=/tmp/.uv-cache \
                    -e DATABASE_URL="postgresql+psycopg://modelmatch:modelmatch@localhost:${pgPort}/modelmatch" \
                    -e JWT_SECRET="${jwt}" -e LLM_CLIENT=fake \
                    -v "\$WORKSPACE":/work -w /work \
                    "\$UV_IMAGE" \
                    bash -lc 'uv run --no-sync pytest -m integration -q'
                """
              }
            }
          }
          post {
            always {
              sh 'COMPOSE_PROJECT_NAME=mm-be-contract-${RUN_ID} ./ci/e2e-stack.sh down || true'
            }
          }
        }

        stage('Container Integration (BE image, HTTP smoke)') {
          // P31 boundary smoke: prove the freshly-built backend IMAGE (gunicorn + DB
          // driver + entrypoint + migrations + seed data) actually serves the API over
          // HTTP. No FE, no Playwright — a thin curl/python smoke against /readyz, the
          // auth round-trip, the seeded recommender, and one /projects write+read.
          // Isolated compose project name + free ports + `down -v` cleanup. Fake LLM.
          steps {
            script {
              env.FAILED_STAGE = 'Container Integration (BE image, HTTP smoke)'
              def ports = sh(script: './ci/free-ports.sh 2', returnStdout: true).trim().split(/\s+/)
              String bePort = ports[0]
              String pgPort = ports[1]
              String jwt = sh(script: 'openssl rand -hex 32', returnStdout: true).trim()

              withEnv([
                "COMPOSE_PROJECT_NAME=mm-be-cint-${env.RUN_ID}",
                "BACKEND_IMAGE=${env.ECR_REGISTRY}/${env.ECR_REPO}:${env.IMAGE_CANDIDATE}",
                "JWT_SECRET=${jwt}",
                "BACKEND_PORT=${bePort}",
                "POSTGRES_PORT=${pgPort}",
                // No FE involved; pin PUBLIC_BASE_URL / CORS at the backend port so the
                // container is internally self-consistent without a browser origin.
                "PUBLIC_BASE_URL=http://localhost:${bePort}",
                "CORS_ALLOW_ORIGINS=http://localhost:${bePort}",
                "LLM_CLIENT=fake",
              ]) {
                sh './ci/e2e-stack.sh up-backend'
                sh """
                  set -eu
                  E2E_API_BASE="http://localhost:${bePort}" BUILD_NUMBER="${env.BUILD_NUMBER}" \
                    ./ci/integration-smoke.sh
                """
              }
            }
          }
          post {
            always {
              sh 'COMPOSE_PROJECT_NAME=mm-be-cint-${RUN_ID} ./ci/e2e-stack.sh down || true'
            }
          }
        }

        stage('E2E (throwaway compose, fake LLM)') {
          // Drive the candidate backend through the real FE/API path: throwaway
          // FE(ECR)+BE(candidate)+Postgres stack -> migrate+seed -> Playwright smoke. Fake LLM.
          steps {
            script {
              env.FAILED_STAGE = 'E2E (throwaway compose, fake LLM)'
              def ports = sh(script: './ci/free-ports.sh 3', returnStdout: true).trim().split(/\s+/)
              String fePort = ports[0]
              String bePort = ports[1]
              String pgPort = ports[2]
              String jwt = sh(script: 'openssl rand -hex 32', returnStdout: true).trim()

              withEnv([
                "COMPOSE_PROJECT_NAME=mm-be-e2e-${env.RUN_ID}",
                "BACKEND_IMAGE=${env.ECR_REGISTRY}/${env.ECR_REPO}:${env.IMAGE_CANDIDATE}",
                "FRONTEND_IMAGE=${env.ECR_REGISTRY}/${env.E2E_FRONTEND_REPO}:${env.E2E_FRONTEND_TAG}",
                "JWT_SECRET=${jwt}",
                "FRONTEND_PORT=${fePort}",
                "BACKEND_PORT=${bePort}",
                "POSTGRES_PORT=${pgPort}",
                "API_BASE_URL=http://localhost:${bePort}",    // SPA -> backend (browser-facing)
                "PUBLIC_BASE_URL=http://localhost:${bePort}", // ci-setup / ingest URL
                "CORS_ALLOW_ORIGINS=http://localhost:${fePort}",
                "LLM_CLIENT=fake",
              ]) {
                // ECR login so compose can pull the pinned frontend image (instance role).
                sh '''
                  set -eu
                  aws ecr get-login-password --region "$AWS_DEFAULT_REGION" \
                    | docker login --username AWS --password-stdin "$ECR_REGISTRY"
                '''
                sh './ci/e2e-stack.sh up'
                // Playwright in its pinned image on host network; E2E_REQUIRE_BACKEND makes
                // an unreachable backend fail rather than skip.
                sh """
                  set -eu
                  docker run --rm --network host \
                    -u "\$(id -u):\$(id -g)" -e HOME=/tmp \
                    -e E2E_REQUIRE_BACKEND=true \
                    -e E2E_BASE_URL="http://localhost:${fePort}" \
                    -e E2E_API_BASE="http://localhost:${bePort}" \
                    -v "\$WORKSPACE/ci/e2e":/work -w /work \
                    "\$PLAYWRIGHT_IMAGE" \
                    bash -lc 'npm ci && npx playwright test --config playwright.config.ts'
                """
              }
            }
          }
          post {
            always {
              sh 'COMPOSE_PROJECT_NAME=mm-be-e2e-${RUN_ID} ./ci/e2e-stack.sh down || true'
            }
          }
        }

        stage('E2E live (gated real-Bedrock)') {
          // The single real-model path (main only). Same stack with LLM_CLIENT=bedrock;
          // the subcheck makes one real Nova call via the instance profile. Token-capped.
          when { branch 'main' }
          steps {
            script {
              env.FAILED_STAGE = 'E2E live (gated real-Bedrock)'
              def ports = sh(script: './ci/free-ports.sh 3', returnStdout: true).trim().split(/\s+/)
              String fePort = ports[0]
              String bePort = ports[1]
              String pgPort = ports[2]
              String jwt = sh(script: 'openssl rand -hex 32', returnStdout: true).trim()
              String roPw = sh(script: 'openssl rand -hex 24', returnStdout: true).trim()

              withEnv([
                "COMPOSE_PROJECT_NAME=mm-be-live-${env.RUN_ID}",
                "BACKEND_IMAGE=${env.ECR_REGISTRY}/${env.ECR_REPO}:${env.IMAGE_CANDIDATE}",
                "FRONTEND_IMAGE=${env.ECR_REGISTRY}/${env.E2E_FRONTEND_REPO}:${env.E2E_FRONTEND_TAG}",
                "JWT_SECRET=${jwt}",
                "FRONTEND_PORT=${fePort}",
                "BACKEND_PORT=${bePort}",
                "POSTGRES_PORT=${pgPort}",
                "API_BASE_URL=http://localhost:${bePort}",
                "PUBLIC_BASE_URL=http://localhost:${bePort}",
                "CORS_ALLOW_ORIGINS=http://localhost:${fePort}",
                // Live surface: Bedrock Nova via instance profile. A real chat RO password is
                // required once LLM_CLIENT!=fake; migrate creates the role with it (random per run).
                "LLM_CLIENT=bedrock",
                "CHAT_READONLY_DB_PASSWORD=${roPw}",
              ]) {
                sh '''
                  set -eu
                  aws ecr get-login-password --region "$AWS_DEFAULT_REGION" \
                    | docker login --username AWS --password-stdin "$ECR_REGISTRY"
                '''
                sh './ci/e2e-stack.sh up'
                sh """
                  set -eu
                  E2E_API_BASE="http://localhost:${bePort}" ./ci/e2e-live-check.sh
                """
              }
            }
          }
          post {
            always {
              sh 'COMPOSE_PROJECT_NAME=mm-be-live-${RUN_ID} ./ci/e2e-stack.sh down || true'
            }
          }
        }
      }
    }

    // ===================== RELEASE tail (main only) =====================

    stage('Release (main)') {
      when { allOf { branch 'main'; expression { !params.FAST_ONLY } } }
      stages {
        stage('Tag') {
          // Compute the next SemVer and create an annotated tag on HEAD (BE write key).
          // Idempotent: already tagged here -> reuse; computed tag on another commit -> fail.
          steps {
            withCredentials([sshUserPrivateKey(credentialsId: env.CRED_BE_DEPLOY_KEY,
                                               keyFileVariable: 'BE_KEY',
                                               usernameVariable: 'BE_USER')]) {
              script {
                env.FAILED_STAGE = 'Tag'
                env.RELEASE_VERSION = sh(returnStdout: true, script: '''
                  set -eu
                  export GIT_SSH_COMMAND="ssh -i $BE_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
                  git remote set-url origin "$BE_REPO_SSH"
                  git fetch --tags --force origin >/dev/null 2>&1
                  HEAD_SHA=$(git rev-parse HEAD^{commit})

                  # already tagged on THIS commit -> idempotent reuse, no new tag
                  EXISTING=$(git tag --points-at HEAD | grep -E "^v[0-9]+\\.[0-9]+\\.[0-9]+$" | sort -V | tail -1 || true)
                  if [ -n "$EXISTING" ]; then
                    echo "idempotent: HEAD already tagged $EXISTING" >&2
                    printf "%s" "${EXISTING#v}"; exit 0
                  fi

                  # next = bump patch of the highest existing vX.Y.Z (or v0.0.1 if none)
                  LATEST=$(git tag -l "v*" | grep -E "^v[0-9]+\\.[0-9]+\\.[0-9]+$" | sort -V | tail -1 || true)
                  if [ -z "$LATEST" ]; then
                    NEXT="v0.0.1"
                  else
                    MJ=$(echo "$LATEST" | sed -E "s/^v([0-9]+)\\.([0-9]+)\\.([0-9]+)$/\\1/")
                    MI=$(echo "$LATEST" | sed -E "s/^v([0-9]+)\\.([0-9]+)\\.([0-9]+)$/\\2/")
                    PA=$(echo "$LATEST" | sed -E "s/^v([0-9]+)\\.([0-9]+)\\.([0-9]+)$/\\3/")
                    NEXT="v${MJ}.${MI}.$((PA + 1))"
                  fi

                  # guard: NEXT must not already exist on a DIFFERENT commit
                  if git rev-parse -q --verify "refs/tags/${NEXT}" >/dev/null 2>&1; then
                    TAGGED=$(git rev-list -n 1 "${NEXT}")
                    if [ "$TAGGED" != "$HEAD_SHA" ]; then
                      echo "FAIL: ${NEXT} already exists on ${TAGGED}, not ${HEAD_SHA}" >&2
                      exit 1
                    fi
                    echo "idempotent: ${NEXT} already on HEAD" >&2
                    printf "%s" "${NEXT#v}"; exit 0
                  fi

                  git config user.email "jenkins@modelmatch.ci"
                  git config user.name  "modelmatch-jenkins"
                  git tag -a "$NEXT" -m "release: $NEXT (build ${BUILD_NUMBER})" "$HEAD_SHA"
                  git push origin "refs/tags/${NEXT}" >/dev/null 2>&1
                  echo "created annotated tag $NEXT on $HEAD_SHA" >&2
                  printf "%s" "${NEXT#v}"
                ''').trim()
                echo "Release version: ${env.RELEASE_VERSION}"
                currentBuild.displayName = "#${env.BUILD_NUMBER} v${env.RELEASE_VERSION}"
              }
            }
          }
        }

        stage('Publish (ECR)') {
          // Promote the scanned candidate image to the SemVer tag (instance role).
          steps {
            script { env.FAILED_STAGE = 'Publish (ECR)' }
            sh '''
              set -eu
              aws ecr get-login-password --region "$AWS_DEFAULT_REGION" \
                | docker login --username AWS --password-stdin "$ECR_REGISTRY"
              docker tag  "$ECR_REGISTRY/$ECR_REPO:$IMAGE_CANDIDATE" "$ECR_REGISTRY/$ECR_REPO:$RELEASE_VERSION"
              docker push "$ECR_REGISTRY/$ECR_REPO:$RELEASE_VERSION"
              echo "Published $ECR_REPO:$RELEASE_VERSION"
            '''
          }
        }

        stage('Deploy (gitops bump)') {
          // The only deploy action: bump the backend app image tag AND the migrate-job
          // image tag in gitops, then push; ArgoCD syncs from there. Never a hand kubectl/helm.
          steps {
            script { env.FAILED_STAGE = 'Deploy (gitops bump)' }
            withCredentials([sshUserPrivateKey(credentialsId: env.CRED_GITOPS_KEY,
                                               keyFileVariable: 'GITOPS_KEY',
                                               usernameVariable: 'GITOPS_USER')]) {
              sh '''
                set -eu
                export GIT_SSH_COMMAND="ssh -i $GITOPS_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
                rm -rf gitops-deploy
                git clone "$GITOPS_REPO" gitops-deploy
                cd gitops-deploy
                docker run --rm -u "$(id -u):$(id -g)" -v "$PWD":/w -w /w "$YQ_IMAGE" \
                  eval -i ".backend.image.tag = \\"$RELEASE_VERSION\\"" "$GITOPS_VALUES"
                docker run --rm -u "$(id -u):$(id -g)" -v "$PWD":/w -w /w "$YQ_IMAGE" \
                  eval -i ".migrate.image.tag = \\"$RELEASE_VERSION\\"" "$GITOPS_MIGRATE_VALUES"
                if git diff --quiet -- "$GITOPS_VALUES" "$GITOPS_MIGRATE_VALUES"; then
                  echo "gitops already at backend.image.tag and migrate.image.tag = $RELEASE_VERSION — nothing to commit"
                else
                  git config user.email "jenkins@modelmatch.ci"
                  git config user.name  "modelmatch-jenkins"
                  git add "$GITOPS_VALUES" "$GITOPS_MIGRATE_VALUES"
                  git commit -m "deploy(backend): image tag -> $RELEASE_VERSION (build ${BUILD_NUMBER})"
                  git push origin HEAD:main
                  echo "Bumped gitops backend.image.tag + migrate.image.tag -> $RELEASE_VERSION"
                fi
              '''
            }
          }
        }
      }
    }
  }

  post {
    always {
      // Remove the per-build candidate image so the persistent controller doesn't accumulate
      // layers on disk. Guarded: IMAGE_CANDIDATE is unset if we failed before Source+config.
      sh 'if [ -n "${IMAGE_CANDIDATE:-}" ]; then docker image rm -f "$ECR_REGISTRY/$ECR_REPO:$IMAGE_CANDIDATE" || true; fi'
    }
    success {
      echo "P18 backend pipeline GREEN on ${env.BRANCH_NAME}"
      script { notifySlack(true) }
    }
    failure {
      script { notifySlack(false) }
    }
  }
}
