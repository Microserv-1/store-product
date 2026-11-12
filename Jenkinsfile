pipeline {
  agent { label 'docker-kubectl' }
  options { timestamps(); disableConcurrentBuilds() }
  parameters {
    string(name: 'DOCKERHUB_NAMESPACE', defaultValue: '', description: 'Required Docker Hub namespace')
    booleanParam(name: 'DEPLOY', defaultValue: false, description: 'Deploy using the dedicated kubeconfig credential')
  }
  stages {
    stage('Checkout') { steps { checkout scm } }
    stage('Test') {
      steps {
        sh '''
          docker run --rm \
            -v "$PWD:/src:ro" \
            -e PYTHONPATH=/src \
            -w /tmp \
            python:3.12-slim \
            sh -ec '
              pip install -r /src/requirements.txt
              python -m pytest /src/tests -q -p no:cacheprovider
            '
        '''
      }
    }
    stage('Build') {
      steps {
        script {
          if (!params.DOCKERHUB_NAMESPACE.matches('[a-z0-9][a-z0-9_-]*')) { error('Invalid registry namespace') }
          env.IMAGE_TAG = sh(script: 'git rev-parse HEAD', returnStdout: true).trim()
          env.IMAGE = "${params.DOCKERHUB_NAMESPACE}/store-product:${env.IMAGE_TAG}"
        }
        sh 'docker build -t "$IMAGE" .'
      }
    }
    stage('Publish') {
      steps {
        withCredentials([usernamePassword(credentialsId: 'dockerhub', usernameVariable: 'REGISTRY_USER', passwordVariable: 'REGISTRY_PASSWORD')]) {
          sh '''
            set +x
            export DOCKER_CONFIG=$(mktemp -d)
            trap 'rm -rf "$DOCKER_CONFIG"' EXIT
            printf '%s' "$REGISTRY_PASSWORD" | docker login -u "$REGISTRY_USER" --password-stdin
            docker push "$IMAGE"
          '''
        }
      }
    }
    stage('Deploy') {
      when { expression { params.DEPLOY } }
      steps {
        withCredentials([file(credentialsId: 'store-kubeconfig', variable: 'KUBECONFIG')]) {
          sh '''
            set -eu
            sed "s|image: microserv-store/product:local|image: $IMAGE|" k8s/k8s.yaml > deployment.rendered.yaml
            kubectl apply --dry-run=server -f deployment.rendered.yaml
            kubectl apply -f deployment.rendered.yaml
            kubectl rollout status deployment/product -n store --timeout=180s
          '''
        }
      }
    }
  }
  post { always { archiveArtifacts artifacts: 'deployment.rendered.yaml', allowEmptyArchive: true } }
}
