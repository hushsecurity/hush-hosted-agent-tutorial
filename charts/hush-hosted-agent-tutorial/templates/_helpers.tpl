{{- define "tutorial.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "tutorial.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- /* A component's resource name; takes (list $ component). */ -}}
{{- define "tutorial.componentName" -}}
{{- printf "%s-%s" (include "tutorial.fullname" (index . 0)) (index . 1) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- /* Labels that pick a component's pods; takes (list $ component). */ -}}
{{- define "tutorial.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tutorial.name" (index . 0) }}
app.kubernetes.io/instance: {{ (index . 0).Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{- define "tutorial.labels" -}}
{{ include "tutorial.selectorLabels" . }}
helm.sh/chart: {{ printf "%s-%s" (index . 0).Chart.Name (index . 0).Chart.Version | replace "+" "_" }}
app.kubernetes.io/managed-by: {{ (index . 0).Release.Service }}
{{- end -}}

{{- define "tutorial.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}

{{- /* Whether Expenses is served through a quick tunnel. */ -}}
{{- define "tutorial.tunnel" -}}
{{- if not .Values.expenses.url }}true{{ end -}}
{{- end -}}

{{- define "tutorial.expensesSecret" -}}
{{- include "tutorial.componentName" (list . "expenses") -}}
{{- end -}}

{{- /*
A random value (128 bits) kept in the Expenses Secret under `key`: the one the
release already has, else a new one. Takes (list $ key).
*/ -}}
{{- define "tutorial.expensesSecretValue" -}}
{{- $ := index . 0 -}}
{{- $key := index . 1 -}}
{{- $secret := lookup "v1" "Secret" $.Release.Namespace (include "tutorial.expensesSecret" $) -}}
{{- $data := dig "data" dict ($secret | default dict) -}}
{{- if hasKey $data $key -}}
{{- index $data $key | b64dec -}}
{{- else -}}
{{- randAlphaNum 22 -}}
{{- end -}}
{{- end -}}

{{- /* Settings that meet the restricted Pod Security Standard. */ -}}
{{- define "tutorial.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 65532
runAsGroup: 65532
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "tutorial.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{- define "tutorial.validate" -}}
{{- range $value, $name := dict "url" "gateway.url" "issuer" "gateway.issuer" -}}
{{- if not (index $.Values.gateway $value) -}}
{{- fail (printf "Set %s. See values.yaml for where to find it." $name) -}}
{{- end -}}
{{- end -}}
{{- range $name, $url := dict "gateway.url" .Values.gateway.url "gateway.issuer" .Values.gateway.issuer "expenses.url" .Values.expenses.url -}}
{{- if and $url (not (hasPrefix "https://" $url)) -}}
{{- fail (printf "%s must be an https:// address (got %s)." $name $url) -}}
{{- end -}}
{{- end -}}
{{- end -}}
