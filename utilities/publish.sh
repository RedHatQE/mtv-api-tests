# Create a wildcard Certificate Signed by a generated unknown root CA key
# Configure openshift Ingress to use this certificate
# Wait for forklift ui pod to accept this certificate when provider by oauth
#
# see openshift documentation for more information:
#    https://docs.openshift.com/container-platform/4.10/security/certificates/replacing-default-ingress-certificate.html
restore="$1"

WAIT_INTERVAL_SEC=10
FAIL_AFTER=50 # wait  50 * 10 second before falling


# the wildcard certification  (*.apps.<cluster>.domain) requires a subjectAltName extension  DNS1.->apps.<cluster>.domain
echo apiVersion: config.openshift.io/v1 > ./ingress.yaml
echo kind: Ingress >> ./ingress.yaml
echo metadata: >> ./ingress.yaml
echo "  name: cluster" >> ./ingress.yaml
apps_domain=$(oc get --no-headers -A -o custom-columns=C:.spec.domain -f ./ingress.yaml)

wildcard_domain=*.$apps_domain
oauthrout=oauth-openshift.$apps_domain

cert_org="MigrationQE"
orig_ca_cn="ingress-operator"
cert_search_string=$cert_org

function generate_certificate {

    echo Create Root Key
    openssl genrsa -passout pass:12345678  -des3 -out rootCA.key 4096

    echo Create and self sign the Root Certificate
    openssl req -x509 -new -nodes -key rootCA.key -sha256 -days 1024 -out rootCA.crt -passin pass:12345678 -batch

    echo Create the certificate key
    openssl genrsa -passout pass:12345678 -out testdomain.com.key 2048

    echo Create the signing csr for "$wildcard_domain"
    openssl req -new -sha256 -key testdomain.com.key -subj "/C=US/ST=CA/O=$cert_org, Inc./CN=$wildcard_domain" -out testdomain.com.csr

    echo Generate the certificate
    echo subjectAltName = @alt_names > ./extfile
    echo [alt_names] >> ./extfile
    echo DNS.1 = "$wildcard_domain" >> ./extfile
    openssl x509 -extfile extfile -req -in testdomain.com.csr -CA rootCA.crt -passin pass:12345678 -CAkey rootCA.key -CAcreateserial -out testdomain.com.crt -days 500 -sha256

    echo New Certificate Content
    openssl x509 -in testdomain.com.crt -text -noout

}

function upload_certificates_and_key_to_openshift {

    # see openshift documentation for more information:
    # https://docs.openshift.com/container-platform/4.10/security/certificates/replacing-default-ingress-certificate.html

    echo publish certificate in Openshift
    oc delete configmap custom-ca -n openshift-config

    oc create configmap custom-ca \
         --from-file=ca-bundle.crt=rootCA.crt \
         -n openshift-config

    oc patch proxy/cluster \
         --type=merge \
         --patch='{"spec":{"trustedCA":{"name":"custom-ca"}}}'

    echo configuration ingress
    oc delete secret ingress-custom-root-ca -n openshift-ingress

    oc create secret tls ingress-custom-root-ca \
         --cert=testdomain.com.crt \
         --key=testdomain.com.key \
         -n openshift-ingress


    # To ensure reconsolidation happens in case the secret was already set
    oc patch ingresscontroller.operator default \
         --type=merge -p \
         '{"spec":{"defaultCertificate": {"name": "for-recon-temp"}}}' \
         -n openshift-ingress-operator

    oc patch ingresscontroller.operator default \
         --type=merge -p \
         '{"spec":{"defaultCertificate": {"name": "ingress-custom-root-ca"}}}' \
         -n openshift-ingress-operator

    }

function wait_for_forklift {

    echo Trace the ssl certification from forklift pods until the new/origin certificat is used and accepteded.
    is_expected_cert=''
    cert_err=''

    loop_limit=$FAIL_AFTER
    while [[ -z $is_expected_cert  && $loop_limit > -1 ]]
    do
      echo waiting for cert...
      forklift_ui_pod_name=$(oc get pod -nopenshift-mtv |grep forklift-ui|awk '{print $1}')
      (( "loop_limit=loop_limit-1" ))
      sleep $WAIT_INTERVAL_SEC
      is_expected_cert=$(oc rsh -nopenshift-mtv "$forklift_ui_pod_name"  openssl s_client -showcerts -servername  oauth-openshift."$apps_domain" -connect  oauth-openshift."$apps_domain":443 </dev/null|grep $cert_search_string)
      cert_err=$(oc rsh -nopenshift-mtv "$forklift_ui_pod_name"  openssl s_client -showcerts -servername  oauth-openshift."$apps_domain" -connect oauth-openshift."$apps_domain":443 </dev/null|grep 'unable to verify the first certificate')
    done

    if [[  -z $is_expected_cert ]]
    then
      echo timeout out... giving up waiting for the cert to be used.
      exit 1
    fi

    if [[ ! -z $cert_err ]]
    then
      echo error! certificat is not accepteded
      exit 1
    fi
}

if [[ ! -z $restore && $restore == "restore" ]]
then
     echo "restoring..."
     cert_search_string=$orig_ca_cn
     oc delete configmap custom-ca  -n openshift-config
     oc patch ingresscontroller.operator default --type json -p '[{ "op": "remove", "path": "/spec/defaultCertificate" }]' -n openshift-ingress-operator
     oc patch proxy/cluster \
          --type=merge \
          --patch='{"spec":{"trustedCA":{"name":""}}}'

     wait_for_forklift
     exit
fi

generate_certificate
upload_certificates_and_key_to_openshift
wait_for_forklift

echo done.
echo to restore run "$0" restore
